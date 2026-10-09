#!/usr/bin/env python3
"""torr2strm 1.4.0 - TorrServer -> multiple STRM materialized trees.

Key rules:
- TorrServer is the source of truth.
- One discovery/enrichment pass is shared by all output trees.
- Category is normalized to movie/tv; an unknown category may be resolved from an exact JacRed match; anime stays _uncategorized.
- Each output tree has its own root, marker and manifest.
- Jellyfin output: movie and TV are file-level STRM using TorrServer /play/{hash}/{file_id}.
- Kodi/Elementum output: movies are torrent-level STRM; TV is file-level STRM using Elementum + oindex (zero-based FileStats order).
- Both outputs receive the same NFO content for the represented media file.
- Quality is classified only from real ffprobe dimensions: 4K or 1080p.
- NFO contains identification/base metadata plus fileinfo/streamdetails.
- Existing valid NFOs are reusable media-info cache across output trees.
- Only Jellyfin output can have reverse deletion of the TorrServer torrent; Kodi output is read-only.
- Reverse deletion uses TorrServer action=rem only; never action=drop.
- Recoverable per-torrent probe failures do not make the service exit non-zero.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import tomllib
from dataclasses import dataclass, replace
from typing import Any

VERSION = "1.4.0"
MANIFEST_VERSION = 5
NFO_FORMAT_VERSION = 3
LOG = logging.getLogger("torr2strm")

VIDEO_EXTENSIONS = {
    ".3g2", ".3gp", ".avi", ".asf", ".asx", ".divx", ".dv", ".dvr-ms",
    ".f4v", ".flv", ".m2t", ".m2ts", ".m2v", ".m4v", ".mkv", ".mk3d",
    ".mov", ".mp4", ".mpe", ".mpeg", ".mpg", ".mts", ".mxf", ".nsv",
    ".nuv", ".ogm", ".ogv", ".pva", ".qt", ".rec", ".rm", ".rmvb",
    ".strm", ".svq3", ".tp", ".ts", ".ty", ".vob", ".webm", ".wmv",
    ".wtv", ".xvid"
}


def norm_key(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch not in "_- ")


def get_field(obj: dict[str, Any], *names: str, default: Any = None) -> Any:
    lookup = {norm_key(str(k)): v for k, v in obj.items()}
    for name in names:
        key = norm_key(name)
        if key in lookup:
            return lookup[key]
    return default


def canonical_hash(value: str) -> str:
    return value.strip().lower()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def valid_id(value: Any, kind: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"0", "null", "none"}:
        return None
    if kind == "tmdb":
        return text if text.isdigit() else None
    if kind == "imdb":
        return text if re.fullmatch(r"tt\d+", text, flags=re.IGNORECASE) else None
    return None


def extract_magnet(value: Any) -> str | None:
    """Find a magnet URI in nested metadata without inventing non-magnet URLs."""
    if value is None:
        return None
    if isinstance(value, str):
        text = html.unescape(value).strip()
        m = re.search(r'magnet:\?[^\s<>"\']+', text, flags=re.IGNORECASE)
        if m:
            return m.group(0).rstrip(",;)")
        return None
    if isinstance(value, dict):
        # Prefer fields that are semantically expected to carry the original link.
        for key in ("magnet", "magnetUrl", "link", "url", "downloadUrl", "torrentUrl"):
            found = extract_magnet(value.get(key))
            if found:
                return found
        for child in value.values():
            found = extract_magnet(child)
            if found:
                return found
        return None
    if isinstance(value, (list, tuple)):
        for child in value:
            found = extract_magnet(child)
            if found:
                return found
    return None


def constructed_magnet(torrent_hash: str, title: str) -> str:
    """Build a valid BTIH magnet when no original/full magnet is available."""
    dn = urllib.parse.quote(str(title or torrent_hash), safe="")
    return f"magnet:?xt=urn:btih:{canonical_hash(torrent_hash)}&dn={dn}"


def sanitize_component(value: str) -> str:
    """Make a safe single Linux/Jellyfin filename component without mangling readable punctuation."""
    value = str(value or "").replace("\x00", "")
    # On Linux only slash is a path separator. Preserve release punctuation
    # such as |, :, ?, *, and backslash.
    value = value.replace("/", " - ")
    value = "".join(ch if ord(ch) >= 32 else " " for ch in value)
    value = re.sub(r"\s+", " ", value).strip().rstrip(".")
    return value or "_"


def truncate_utf8(value: str, max_bytes: int) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    cut = raw[:max_bytes]
    while cut:
        try:
            return cut.decode("utf-8")
        except UnicodeDecodeError:
            cut = cut[:-1]
    return "_"


def extract_tv_series_name(title: str, files: tuple[TorrentFile, ...]) -> str:
    """Derive a readable series name from release title/path without external APIs."""
    text = str(title or "").strip()
    patterns = [
        r"\s+\d{1,2}\s+(?:сезон|season)\b.*$",
        r"\s+(?:S|Season|Сезон)\s*\d{1,2}(?:E\d{1,3}(?:[-,]\d{1,3})?)?\b.*$",
        r"\s+(?:Серии)\s+\d{1,3}(?:[-,]\d{1,3})?(?:\s+из\s+\d{1,3})?\b.*$",
    ]
    for pattern in patterns:
        candidate = re.sub(pattern, "", text, flags=re.IGNORECASE).strip(" -_.,|/\\")
        if candidate and candidate != text:
            return candidate
    for f in sorted(files, key=lambda x: x.path):
        base = Path(f.path).name
        m = re.search(r"(?:^|[ ._-])S\d{1,2}E\d{1,3}\b", base, flags=re.IGNORECASE)
        if m:
            candidate = base[:m.start()].strip(" ._-")
            if candidate:
                return candidate.replace(".", " ").replace("_", " ").strip()
    return text



def extract_btih(value: Any) -> str | None:
    """Extract and normalize a 40-char hex or 32-char base32 BTIH from text."""
    if value is None:
        return None
    text = urllib.parse.unquote(str(value)).strip()
    match = re.search(r"(?:urn:btih:|btih:)([A-Za-z0-9]{32,40})", text, flags=re.IGNORECASE)
    if not match:
        return None
    token = match.group(1)
    if re.fullmatch(r"[0-9A-Fa-f]{40}", token):
        return token.lower()
    if re.fullmatch(r"[A-Za-z2-7]{32}", token, flags=re.IGNORECASE):
        try:
            padded = token.upper() + "=" * ((8 - len(token) % 8) % 8)
            return base64.b32decode(padded).hex()
        except Exception:
            return None
    return None


def quality_from_dimensions(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        raise ValueError(f"invalid video dimensions {width}x{height}")
    # Use 4K-class horizontal/vertical dimensions, not the generic 2160 threshold:
    # 2560x1440 is 1440p and belongs in the "1080p" (everything else) root.
    return "4K" if max(width, height) >= 3840 else "1080p"


def usable_ffprobe(payload: Any) -> dict[str, Any] | None:
    """Accept JacRed/TorrServer ffprobe JSON and require a real video stream."""
    if isinstance(payload, list):
        streams = payload
    elif isinstance(payload, dict):
        streams = payload.get("streams")
    else:
        return None
    if not isinstance(streams, list) or not streams:
        return None
    valid_video = []
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        if str(stream.get("codec_type", "")).lower() != "video":
            continue
        try:
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
        except (TypeError, ValueError):
            continue
        if width > 0 and height > 0 and str(stream.get("codec_name") or "").strip():
            valid_video.append(stream)
    if not valid_video:
        return None
    return {"streams": streams}


def ffprobe_video_stream(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = usable_ffprobe(payload)
    if normalized is None:
        raise ValueError("ffprobe payload has no usable video stream")
    for stream in normalized["streams"]:
        if isinstance(stream, dict) and str(stream.get("codec_type", "")).lower() == "video":
            return stream
    raise ValueError("ffprobe payload has no video stream")


def media_quality(payload: dict[str, Any]) -> str:
    stream = ffprobe_video_stream(payload)
    try:
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid video dimensions in ffprobe") from exc
    return quality_from_dimensions(width, height)




def quality_label_from_dimensions(width: int, height: int, stream: dict[str, Any] | None = None) -> str | None:
    """Return a normalized human-facing resolution/HDR label from real stream data."""
    if width <= 0 or height <= 0:
        return None
    maximum = max(width, height)
    if maximum >= 7680:
        label = "4320p"
    elif maximum >= 3840:
        label = "2160p"
    elif maximum >= 2560:
        label = "1440p"
    elif maximum >= 1920:
        label = "1080p"
    elif maximum >= 1280:
        label = "720p"
    elif maximum >= 640:
        label = "480p"
    elif maximum >= 426:
        label = "360p"
    elif maximum >= 240:
        label = "240p"
    else:
        return None

    fields = stream if isinstance(stream, dict) else {}
    field_order = str(fields.get("field_order") or fields.get("fieldOrder") or "").lower()
    scan_type = str(fields.get("scantype") or fields.get("scan_type") or "").lower()
    tags = fields.get("tags") if isinstance(fields.get("tags"), dict) else {}
    transfer = str(fields.get("color_transfer") or fields.get("transfer_characteristics") or "").lower()
    if not transfer:
        transfer = str(next((v for k, v in tags.items() if str(k).lower() in {"color_transfer", "transfer_characteristics"}), "") or "").lower()
    title_bits = " ".join(str(v or "") for k, v in tags.items() if str(k).lower() == "title").lower()
    is_interlaced = field_order in {"tt", "bb", "tb", "bt", "tff", "bff"} or scan_type in {"interlaced", "i"}
    if label == "1080p" and is_interlaced:
        label = "1080i"

    dv_profile = fields.get("dv_profile") or fields.get("dovi_config")
    is_dv = dv_profile not in (None, "", 0, False) or "dolby vision" in title_bits or re.search(r"\bDV\b", title_bits, re.IGNORECASE)
    is_hdr = transfer in {"smpte2084", "pq", "arib-std-b67", "arib_std_b67", "hlg"} or fields.get("mastering_display") or fields.get("content_light") or "hdr" in title_bits
    if is_dv:
        label += " DV"
    elif is_hdr:
        label += " HDR"
    return label


def quality_label_from_probe(payload: Any) -> str | None:
    normalized = usable_ffprobe(payload)
    if normalized is None:
        return None
    for stream in normalized["streams"]:
        if isinstance(stream, dict) and str(stream.get("codec_type", "")).lower() == "video":
            try:
                return quality_label_from_dimensions(int(stream.get("width") or 0), int(stream.get("height") or 0), stream)
            except (TypeError, ValueError):
                return None
    return None


def quality_label_from_text(value: Any) -> str | None:
    """Parse only explicit resolution/interlace/HDR markers; source and codec names alone are insufficient."""
    text = html.unescape(str(value or "")).lower()
    if not text.strip():
        return None
    dimensions = re.search(r"(?<!\d)(\d{3,5})\s*[x×]\s*(\d{3,5})(?!\d)", text)
    if dimensions:
        try:
            return quality_label_from_dimensions(int(dimensions.group(1)), int(dimensions.group(2)))
        except (TypeError, ValueError):
            pass
    if re.search(r"(?<![a-z0-9])(?:8k|4320p)(?![a-z0-9])", text):
        label = "4320p"
    elif re.search(r"(?<![a-z0-9])(?:4k|uhd|2160p|2160i)(?![a-z0-9])", text):
        label = "2160p"
    elif re.search(r"(?<![a-z0-9])(?:1440p|2k)(?![a-z0-9])", text):
        label = "1440p"
    elif re.search(r"(?<![a-z0-9])1080i(?![a-z0-9])", text):
        label = "1080i"
    elif re.search(r"(?<![a-z0-9])1080p(?![a-z0-9])", text):
        label = "1080p"
    elif re.search(r"(?<![a-z0-9])720p(?![a-z0-9])", text):
        label = "720p"
    elif re.search(r"(?<![a-z0-9])(?:576p|576i)(?![a-z0-9])", text):
        label = "576p"
    elif re.search(r"(?<![a-z0-9])(?:480p|480i)(?![a-z0-9])", text):
        label = "480p"
    elif re.search(r"(?<![a-z0-9])360p(?![a-z0-9])", text):
        label = "360p"
    elif re.search(r"(?<![a-z0-9])240p(?![a-z0-9])", text):
        label = "240p"
    else:
        return None

    is_dv = bool(re.search(r"\b(?:dolby[ ._-]?vision|dovi|dv)\b", text))
    is_hdr = bool(re.search(r"\b(?:hdr(?:10(?:\+|plus)?)?|hlg)\b", text))
    if is_dv:
        label += " DV"
    elif is_hdr:
        label += " HDR"
    return label


def quality_label_from_metadata(metadata: dict[str, Any] | None) -> str | None:
    """Inspect explicitly named structured quality fields, not generic titles or codec fields."""
    if not isinstance(metadata, dict):
        return None
    accepted = {
        "quality", "qualityname", "qualitylabel", "resolution", "resolutionname",
        "videoquality", "videoresolution", "videotype", "definition",
    }
    for key, value in metadata.items():
        normalized_key = norm_key(str(key))
        if normalized_key in accepted:
            candidates = value if isinstance(value, (list, tuple)) else [value]
            for candidate in candidates:
                if isinstance(candidate, dict):
                    nested = quality_label_from_metadata(candidate)
                    if nested:
                        return nested
                elif candidate not in (None, ""):
                    label = quality_label_from_text(candidate)
                    if label:
                        return label
                    if re.fullmatch(r"\s*(?:4320|2160|1440|1080|720|576|480|360|240)\s*", str(candidate)):
                        label = quality_label_from_text(f"{str(candidate).strip()}p")
                        if label:
                            return label
        if isinstance(value, dict) and normalized_key in {"info", "metadata", "release", "media", "mediainfo", "details", "torrserver", "qualityinfo"}:
            label = quality_label_from_metadata(value)
            if label:
                return label
    return None


def quality_root_from_label(label: str | None) -> str:
    if not label:
        return "1080p"
    match = re.search(r"(?<!\d)(4320|2160|4k|uhd)(?!\d)", label.lower())
    return "4K" if match else "1080p"


def quality_marker(label: str | None) -> str:
    return f" — {label}" if label else ""



def bounded_kodi_leaf(title: str, quality_label: str | None, short_hash: str, file_ordinal: int | None = None) -> str:
    """Keep quality/hash suffixes intact even when a human title exceeds NAME_MAX."""
    prefix = sanitize_component(title)
    suffix = f"{quality_marker(quality_label)} [{short_hash}]"
    if file_ordinal is not None:
        suffix += f" [file{file_ordinal:02d}]"
    max_bytes = 255 - len(".strm".encode("utf-8"))
    budget = max_bytes - len(suffix.encode("utf-8"))
    if budget < 1:
        raise ValueError("quality/hash suffix is too long for a Linux filename")
    prefix = truncate_utf8(prefix, budget).rstrip().rstrip(".") or "_"
    return f"{prefix}{suffix}"

def media_logical_identity(snap: "TorrentSnapshot") -> str | None:
    """Return a trusted provider identity for the logical movie/series, if present."""
    metadata = snap.metadata if isinstance(snap.metadata, dict) else {}
    ids: dict[str, str] = {}
    if snap.category == "tv":
        raw_series_ids = metadata_value(metadata, "seriesProviderIds", "showProviderIds", "tvProviderIds")
        if isinstance(raw_series_ids, dict):
            ids.update(provider_ids_from_mapping(raw_series_ids))
        series_keys = {
            "tmdb": ("seriesTmdbId", "showTmdbId", "tvTmdbId"),
            "tvdb": ("seriesTvdbId", "showTvdbId", "tvdbSeriesId"),
            "imdb": ("seriesImdbId", "showImdbId"),
            "tvmaze": ("seriesTvmazeId", "showTvmazeId"),
            "trakt": ("seriesTraktId", "showTraktId"),
            "kinopoisk": ("seriesKinopoiskId", "showKinopoiskId"),
        }
        for kind, keys in series_keys.items():
            value = metadata_value(metadata, *keys)
            if value not in (None, ""):
                _put_provider_id(ids, kind, value)
    # If a series-scoped ID was supplied, do not let a generic provider ID
    # override which ID defines this series. Generic IDs are only a fallback.
    if not ids:
        ids.update(provider_ids(metadata, snap.title))
    for kind in ("tmdb", "tvdb", "imdb", "tvmaze", "trakt", "kinopoisk", "mal", "anidb", "anilist", "douban", "wikidata"):
        value = ids.get(kind)
        if value:
            return f"{kind}:{value}"
    return None



def provider_ids_for_snapshot(snap: "TorrentSnapshot") -> dict[str, str]:
    """Prefer explicitly series-scoped identifiers for TV NFOs and logical grouping."""
    metadata = snap.metadata if isinstance(snap.metadata, dict) else {}
    ids = provider_ids(metadata, snap.title)
    if snap.category == "tv":
        raw_series_ids = metadata_value(metadata, "seriesProviderIds", "showProviderIds", "tvProviderIds")
        if isinstance(raw_series_ids, dict):
            ids.update(provider_ids_from_mapping(raw_series_ids))
        series_keys = {
            "tmdb": ("seriesTmdbId", "showTmdbId", "tvTmdbId"),
            "tvdb": ("seriesTvdbId", "showTvdbId", "tvdbSeriesId"),
            "imdb": ("seriesImdbId", "showImdbId"),
            "tvmaze": ("seriesTvmazeId", "showTvmazeId"),
            "trakt": ("seriesTraktId", "showTraktId"),
            "kinopoisk": ("seriesKinopoiskId", "showKinopoiskId"),
        }
        for kind, keys in series_keys.items():
            value = metadata_value(metadata, *keys)
            if value not in (None, ""):
                _put_provider_id(ids, kind, value)
    return ids

def source_episode_coordinates(path_value: str, torrent_title: str) -> tuple[int | None, int | None]:
    """Return a season/episode only when explicit coordinates can be parsed."""
    text = f"{path_value} {torrent_title}"
    patterns = (
        r"(?i)\bS(\d{1,2})E(\d{1,4})(?:[-,]\d{1,4})?\b",
        r"(?i)\b(\d{1,2})x(\d{1,4})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return int(match.group(1)), int(match.group(2))
    season = season_number(path_value, torrent_title)
    if season is None:
        season_match = re.search(r"(?i)\bS(\d{1,2})(?!\s*E\d)", text)
        if season_match:
            season = int(season_match.group(1))
    return season, None


def display_year(metadata: dict[str, Any], title: str, *, tv: bool) -> str | None:
    keys = ("seriesYear", "firstAiredYear", "year", "releaseYear") if tv else ("year", "releaseYear", "releasedYear")
    value = metadata_value(metadata, *keys)
    if value is not None:
        match = re.search(r"\b(?:19|20)\d{2}\b", str(value))
        if match:
            return match.group(0)
    match = re.search(r"\b(?:19|20)\d{2}\b", html.unescape(title or ""))
    return match.group(0) if match else None


def explicit_display_title(snap: "TorrentSnapshot", *, tv: bool) -> tuple[str, str | None, int]:
    metadata = snap.metadata if isinstance(snap.metadata, dict) else {}
    if tv:
        explicit = metadata_value(metadata, "seriesTitle", "seriesName", "showTitle", "showName")
        title = html.unescape(str(explicit)).strip() if explicit not in (None, "") else nfo_series_title(snap)
        priority = 0 if explicit not in (None, "") else 1
    else:
        explicit = metadata_value(metadata, "movieTitle", "movieName", "originalTitle", "originalName")
        title = html.unescape(str(explicit)).strip() if explicit not in (None, "") else nfo_movie_title(snap)
        priority = 0 if explicit not in (None, "") else 1
    # A fallback title may still contain release-quality tokens. Keep those in
    # the final quality label instead of repeating them in the display name.
    title = re.sub(
        r"(?i)(?<![a-z0-9])(?:4320p|2160p|2160i|1440p|1080p|1080i|720p|576p|576i|480p|480i|360p|240p|8k|4k|uhd|2k)(?![a-z0-9])",
        " ", title,
    )
    title = re.sub(r"(?i)\b(?:dolby[ ._-]?vision|dovi|dv|hdr(?:10(?:\+|plus)?)?|hlg)\b", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" ._-—")
    year = display_year(metadata, snap.title, tv=tv)
    if year:
        title = re.sub(rf"\s*\({re.escape(year)}\)\s*$", "", title).strip()
    return sanitize_component(title or snap.title), year, priority


def clean_media_component(title: str, year: str | None) -> str:
    base = sanitize_component(title)
    if year and not re.search(rf"\({re.escape(year)}\)$", base):
        base = sanitize_component(f"{base} ({year})")
    return base


def nfo_xml_value(value: Any) -> str | None:
    if value is None:
        return None
    text = html.unescape(str(value)).strip()
    return text if text else None


def stream_tag_value(stream: dict[str, Any], *keys: str) -> Any:
    tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
    for key in keys:
        if key in stream and stream[key] not in (None, ""):
            return stream[key]
        low = key.lower()
        for tag_key, tag_value in tags.items():
            if str(tag_key).lower() == low and tag_value not in (None, ""):
                return tag_value
    return None


def stream_language(stream: dict[str, Any]) -> str | None:
    value = stream_tag_value(stream, "language")
    if value:
        return str(value).strip().lower()
    title = stream_tag_value(stream, "title")
    if title:
        candidate = str(title).strip().lower()
        if re.fullmatch(r"[a-z]{2,3}", candidate):
            return candidate
    return None


def stream_duration_seconds(stream: dict[str, Any]) -> float | None:
    value = stream_tag_value(stream, "duration")
    if value in (None, ""):
        return None
    text = str(value).strip()
    if ":" in text:
        try:
            parts = text.split(":")
            if len(parts) == 3:
                hours, minutes, seconds = parts
                return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        except (TypeError, ValueError):
            pass
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def stream_bitrate(stream: dict[str, Any]) -> int | None:
    value = stream_tag_value(stream, "bit_rate")
    if value not in (None, ""):
        try:
            n = int(float(value))
            if n > 0:
                return n
        except (TypeError, ValueError):
            pass
    bps = stream_tag_value(stream, "BPS")
    if bps not in (None, ""):
        try:
            n = int(float(str(bps).replace(",", "")))
            if n > 0:
                return n
        except (TypeError, ValueError):
            pass
    return None


def stream_framerate(stream: dict[str, Any]) -> str | None:
    for key in ("avg_frame_rate", "r_frame_rate", "framerate"):
        value = stream_tag_value(stream, key)
        if value in (None, "", "0/0", 0):
            continue
        text = str(value)
        if "/" in text:
            try:
                num, den = text.split("/", 1)
                den_f = float(den)
                if den_f == 0:
                    continue
                return f"{float(num) / den_f:.3f}".rstrip("0").rstrip(".")
            except (TypeError, ValueError):
                continue
        try:
            return f"{float(text):.3f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            continue
    return None


def stream_aspect(stream: dict[str, Any]) -> tuple[str | None, str | None]:
    ratio = stream_tag_value(stream, "display_aspect_ratio", "aspectratio")
    if ratio:
        ratio_text = str(ratio).strip()
        decimal = None
        if ":" in ratio_text:
            try:
                a, b = ratio_text.split(":", 1)
                if float(b) != 0:
                    decimal = f"{float(a) / float(b):.6f}".rstrip("0").rstrip(".")
            except (TypeError, ValueError):
                pass
        else:
            try:
                decimal = f"{float(ratio_text):.6f}".rstrip("0").rstrip(".")
            except (TypeError, ValueError):
                pass
        return decimal, ratio_text
    try:
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        if width <= 0 or height <= 0:
            return None, None
        from math import gcd
        g = gcd(width, height)
        a, b = width // g, height // g
        return f"{width / height:.6f}".rstrip("0").rstrip("."), f"{a}:{b}"
    except (TypeError, ValueError, ZeroDivisionError):
        return None, None


def stream_scantype(stream: dict[str, Any]) -> str | None:
    value = stream_tag_value(stream, "field_order", "scantype")
    if not value:
        return None
    text = str(value).lower()
    if text in {"progressive", "progressive_scan"}:
        return "progressive"
    if text in {"tt", "tb", "bb", "bt", "interlaced", "separated", "telecined", "unknown"}:
        return "interlaced" if text not in {"unknown"} else "unknown"
    return text


def stream_bool(stream: dict[str, Any], name: str) -> bool:
    disposition = stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {}
    value = disposition.get(name)
    if value is None:
        value = stream_tag_value(stream, name)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def add_xml_text(parent: Any, tag: str, value: Any) -> None:
    text = nfo_xml_value(value)
    if text is None:
        return
    child = parent.find(tag)
    if child is None:
        child = parent.makeelement(tag, {}) if hasattr(parent, "makeelement") else None
        if child is None:
            import xml.etree.ElementTree as ET
            child = ET.SubElement(parent, tag)
        else:
            parent.append(child)
    child.text = text


def append_common_stream_fields(node: Any, stream: dict[str, Any]) -> None:
    codec = stream_tag_value(stream, "codec_name")
    add_xml_text(node, "codec", codec)
    add_xml_text(node, "micodec", codec)
    add_xml_text(node, "bitrate", stream_bitrate(stream))
    add_xml_text(node, "language", stream_language(stream))
    add_xml_text(node, "title", stream_tag_value(stream, "title"))
    add_xml_text(node, "default", str(stream_bool(stream, "default")))
    add_xml_text(node, "forced", str(stream_bool(stream, "forced")))
    duration = stream_duration_seconds(stream)
    if duration is not None:
        add_xml_text(node, "duration", str(int(round(duration / 60.0))))
        add_xml_text(node, "durationinseconds", str(int(round(duration))))


def nfo_episode_title(file_path: str) -> str:
    stem = Path(file_path).stem
    episode_no = None
    m = re.search(r"\bS\d{1,2}E(\d{1,4})\b", stem, flags=re.IGNORECASE)
    if m:
        episode_no = m.group(1)
    cleaned = re.sub(r"^.*?S\d{1,2}E\d{1,4}(?:[-_]\d{1,4})?[ ._-]*", "", stem, flags=re.IGNORECASE)
    cleaned = re.sub(r"^.*?\d{1,2}x\d{1,4}[ ._-]*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"\b(?:2160p|1440p|1080p|1080i|720p|576p|480p|4k|8k|WEB[- .]?DL|WEB[- .]?Rip|Blu[- .]?Ray|BDRip|HDTV|HEVC|H\.265|AVC|H\.264)\b.*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = cleaned.replace(".", " ").replace("_", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-_|")
    if cleaned:
        return cleaned
    return f"Episode {episode_no}" if episode_no else stem


def nfo_movie_title(snap: TorrentSnapshot) -> str:
    for key in ("name", "title", "originalname", "originaltitle"):
        value = get_field(snap.metadata, key, default=None)
        if isinstance(value, str) and value.strip():
            return html.unescape(value).strip()
    text = html.unescape(snap.title)
    text = re.split(r"\s*[\[<]", text, maxsplit=1)[0]
    text = re.sub(r"\s+\d{4}\s*$", "", text).strip(" !|/\\-_")
    return text or snap.title


def nfo_series_title(snap: TorrentSnapshot) -> str:
    for key in ("seriesTitle", "seriesName", "name"):
        value = get_field(snap.metadata, key, default=None)
        if isinstance(value, str) and value.strip():
            return html.unescape(value).strip()
    derived = html.unescape(extract_tv_series_name(snap.title, snap.files)).strip()
    if derived:
        return derived
    value = get_field(snap.metadata, "title", default=None)
    return html.unescape(value).strip() if isinstance(value, str) and value.strip() else snap.title


def normalize_provider_type(value: Any) -> str | None:
    text = re.sub(r"[^a-z0-9]", "", str(value or "").strip().lower())
    aliases = {
        "tmdbid": "tmdb", "themoviedb": "tmdb", "tmdb": "tmdb",
        "imdbid": "imdb", "imdb": "imdb",
        "tvdbid": "tvdb", "thetvdb": "tvdb", "tvdb": "tvdb",
        "tvmazeid": "tvmaze", "tvmaze": "tvmaze",
        "traktid": "trakt", "trakt": "trakt",
        "kinopoiskid": "kinopoisk", "kinopoisk": "kinopoisk", "kp": "kinopoisk",
        "malid": "mal", "mal": "mal",
        "anidbid": "anidb", "anidb": "anidb",
        "anilistid": "anilist", "anilist": "anilist",
        "doubanid": "douban", "douban": "douban",
        "wikidataid": "wikidata", "wikidata": "wikidata",
    }
    return aliases.get(text)


def _put_provider_id(result: dict[str, str], provider: Any, value: Any) -> None:
    kind = normalize_provider_type(provider)
    if kind is None:
        # A nested ProviderIds object is already a strong indication that the key
        # names a provider. Preserve unknown provider types as Kodi/Jellyfin-friendly
        # uniqueid types after normalizing their name.
        kind = re.sub(r"[^a-z0-9]", "", str(provider or "").strip().lower())
        if not re.fullmatch(r"[a-z][a-z0-9]{1,39}", kind or ""):
            return
    if value in (None, "", 0, "null", "None"):
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _put_provider_id(result, provider, item)
        return
    text = str(value).strip()
    if kind == "imdb" and not re.fullmatch(r"tt\d+", text, re.IGNORECASE):
        return
    if kind in {"tmdb", "tvdb", "tvmaze", "trakt", "kinopoisk", "mal", "anidb", "anilist", "douban"} and not re.fullmatch(r"[0-9A-Za-z._-]+", text):
        return
    if 1 <= len(text) <= 128:
        result.setdefault(kind, text)


def provider_ids(metadata: dict[str, Any], title: str = "") -> dict[str, str]:
    """Collect known, trustworthy provider IDs from direct/nested metadata and title fallbacks."""
    result: dict[str, str] = {}
    # Arr-style nested providerIds is the most useful source.
    nested = get_field(metadata, "providerIds", "providerids", default=None)
    if isinstance(nested, dict):
        for provider, value in nested.items():
            _put_provider_id(result, provider, value)

    direct_keys = {
        "tmdb": ("tmdbid", "tmdbId", "tmdb"),
        "imdb": ("imdbid", "imdbId", "imdb"),
        "tvdb": ("tvdbid", "tvdbId", "tvdb"),
        "tvmaze": ("tvmazeid", "tvMazeId", "tvmaze"),
        "trakt": ("traktid", "traktId", "trakt"),
        "kinopoisk": ("kinopoiskid", "kinopoiskId", "kinopoisk", "kpId", "kp"),
        "mal": ("malid", "malId"),
        "anidb": ("anidbid", "anidbId"),
        "anilist": ("anilistid", "anilistId"),
        "douban": ("doubanid", "doubanId"),
        "wikidata": ("wikidataid", "wikidataId"),
    }
    for kind, names in direct_keys.items():
        for name in names:
            _put_provider_id(result, kind, get_field(metadata, name, default=None))

    text = html.unescape(str(title or ""))
    if "tmdb" not in result:
        m = re.search(r"\[tmdb(?:id)?[-:]\s*(\d+)\]", text, flags=re.IGNORECASE)
        if m:
            result["tmdb"] = m.group(1)
    if "imdb" not in result:
        m = re.search(r"(?:\[|\b)(tt\d+)(?:\]|\b)", text, flags=re.IGNORECASE)
        if m:
            result["imdb"] = m.group(1)
    if "tvdb" not in result:
        m = re.search(r"\[tvdb(?:id)?[-:]\s*(\d+)\]", text, flags=re.IGNORECASE)
        if m:
            result["tvdb"] = m.group(1)
    return result


def metadata_value(metadata: dict[str, Any], *names: str) -> Any:
    """Return the first non-empty scalar/list metadata field among common Arr/TorrServer names."""
    for name in names:
        value = get_field(metadata, name, default=None)
        if value not in (None, "", [], {}):
            return value
    return None


def metadata_date(metadata: dict[str, Any], *names: str) -> str | None:
    value = metadata_value(metadata, *names)
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return str(int(value))
    text = str(value).strip()
    return text or None


def _nfo_root_tag(content: str) -> str | None:
    m = re.search(r"<(movie|tvshow|episodedetails)\b[^>]*>", content, flags=re.IGNORECASE)
    return m.group(1).lower() if m else None


def _nfo_xml_and_urls(content: str) -> tuple[Any, list[str]]:
    """Parse the XML payload of a normal or Kodi Combination NFO.

    Combination NFOs deliberately contain a URL after the closing XML root,
    so parsing the whole file with ElementTree would reject an otherwise valid
    Kodi NFO. We therefore isolate the root XML before parsing.
    """
    import xml.etree.ElementTree as ET

    root_tag = _nfo_root_tag(content)
    if not root_tag:
        raise ValueError("NFO has no supported XML root")
    open_match = re.search(rf"<{re.escape(root_tag)}\b[^>]*>", content, flags=re.IGNORECASE)
    if not open_match:
        raise ValueError("NFO has no supported XML root opening tag")
    close_token = f"</{root_tag}>"
    close_pos = content.lower().rfind(close_token.lower())
    if close_pos < 0:
        raise ValueError("NFO has no closing XML root")
    close_end = close_pos + len(close_token)
    xml_fragment = content[open_match.start():close_end]
    root = ET.fromstring(xml_fragment)
    tail = content[close_end:]
    urls = [u.strip() for u in re.findall(r"(?m)^\s*(https?://\S+)\s*$", tail) if u.strip()]
    return root, urls


def nfo_format_version(content: str) -> int:
    """Return the torr2strm NFO format version; unversioned legacy NFOs are v1."""
    try:
        root, _ = _nfo_xml_and_urls(content)
    except Exception:
        return 0
    marker = root.find("./torr2strm")
    if marker is None:
        return 1
    raw = marker.get("formatversion") or marker.findtext("formatversion") or marker.text
    try:
        return int(str(raw or "1").strip())
    except (TypeError, ValueError):
        return 1


def _nfo_tmdb_id(root: Any) -> str | None:
    value = root.findtext("./tmdbid")
    if value and str(value).strip().isdigit():
        return str(value).strip()
    for node in root.findall("./uniqueid"):
        if str(node.get("type") or "").strip().lower() == "tmdb":
            value = (node.text or "").strip()
            if value.isdigit():
                return value
    return None


def _nfo_combination_url(root_tag: str, tmdb_id: str | None) -> str | None:
    if not tmdb_id:
        return None
    if root_tag == "movie":
        return f"https://www.themoviedb.org/movie/{urllib.parse.quote(str(tmdb_id), safe='')}"
    if root_tag == "tvshow":
        return f"https://www.themoviedb.org/tv/{urllib.parse.quote(str(tmdb_id), safe='')}"
    return None


def _finalize_nfo(root: Any, *, source_comment: str, combination_url: str | None = None) -> str:
    """Serialize an NFO in the current format, with optional Kodi Combination URL."""
    import xml.etree.ElementTree as ET

    marker = root.find("./torr2strm")
    if marker is None:
        marker = ET.Element("torr2strm", {"formatversion": str(NFO_FORMAT_VERSION)})
        root.insert(0, marker)
    else:
        marker.set("formatversion", str(NFO_FORMAT_VERSION))
        marker.text = None
    xml = ET.tostring(root, encoding="unicode")
    header = f'<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n<!-- generated by torr2strm {VERSION}; {source_comment}; nfo_format={NFO_FORMAT_VERSION} -->\n'
    tail = f"\n{combination_url}" if combination_url else ""
    return f"{header}{xml}{tail}\n"


def migrate_nfo_v1_to_v2(content: str) -> tuple[str, bool]:
    """Migrate a legacy/unversioned torr2strm NFO to format v2 without probing media."""
    version = nfo_format_version(content)
    if version == NFO_FORMAT_VERSION:
        return content, False
    if version > NFO_FORMAT_VERSION:
        LOG.warning("NFO_FORMAT_NEWER version=%s current=%s; preserving content", version, NFO_FORMAT_VERSION)
        return content, False
    root, urls = _nfo_xml_and_urls(content)
    root_tag = str(root.tag).lower()
    # Older schemas contained display metadata. Restrict the upgraded document
    # to the approved allowlist: identity fields, episode coordinates where
    # applicable, and technical stream details. Names must not override locales.
    root_tag = str(root.tag).lower()
    allowed = {"torr2strm", "tmdbid", "imdbid", "tvdbid", "uniqueid"}
    if root_tag == "episodedetails":
        allowed.update({"season", "episode", "fileinfo"})
    elif root_tag == "movie":
        allowed.add("fileinfo")
    for child in list(root):
        if str(child.tag).lower() not in allowed:
            root.remove(child)
    tmdb = _nfo_tmdb_id(root)
    canonical_url = _nfo_combination_url(root_tag, tmdb)
    if canonical_url is None and urls and root_tag in {"movie", "tvshow"}:
        # Preserve an existing scraper hint if we do not have a trustworthy TMDb id.
        canonical_url = urls[0]
    migrated = _finalize_nfo(
        root,
        source_comment="migrated_from_nfo_format=older",
        combination_url=canonical_url,
    )
    return migrated, True


def normalize_cached_nfo(content: str) -> tuple[str, bool]:
    """Upgrade legacy cached NFOs while keeping current/future formats untouched."""
    return migrate_nfo_v1_to_v2(content)


def metadata_original_title(metadata: dict[str, Any], *, tv: bool = False) -> str | None:
    keys = (
        "seriesOriginalTitle", "seriesOriginalName", "originaltitle", "originalTitle", "originalname", "originalName"
    ) if tv else ("originaltitle", "originalTitle", "originalname", "originalName")
    value = metadata_value(metadata, *keys)
    return html.unescape(str(value)).strip() if value not in (None, "") else None


def build_torrent_dir_name(title: str, metadata: dict[str, Any], torrent_hash: str, category: str = "movie", files: tuple[TorrentFile, ...] = ()) -> str:
    base = extract_tv_series_name(title, files) if category == "tv" else title
    parts = [sanitize_component(base)]
    tmdb = provider_ids(metadata, title).get("tmdb")
    imdb = provider_ids(metadata, title).get("imdb")
    # Older manually imported records may contain IDs only in the human title.
    if not tmdb:
        m = re.search(r"\[tmdb(?:id)?[-:]\s*(\d+)\]", title, flags=re.IGNORECASE)
        if m:
            tmdb = m.group(1)
    if not imdb:
        m = re.search(r"\[(tt\d+)\]", title, flags=re.IGNORECASE)
        if m:
            imdb = m.group(1)
    if tmdb:
        parts.append(f"[tmdbid-{tmdb}]")
    if imdb:
        parts.append(f"[imdbid-{imdb}]")
    # The identity suffix must never be truncated: Jellyfin provider IDs and
    # the short hash are the important machine-readable part of this name.
    suffix_parts = []
    if tmdb:
        suffix_parts.append(f"[tmdbid-{tmdb}]")
    if imdb:
        suffix_parts.append(f"[imdbid-{imdb}]")
    suffix_parts.append(f"[{torrent_hash[:8]}]")
    suffix = " ".join(suffix_parts)

    base_prefix = sanitize_component(base)
    max_base_bytes = 255 - 1 - len(suffix.encode("utf-8"))
    if max_base_bytes < 1:
        raise ValueError("torrent identity suffix is too long for a Linux filename")
    base_prefix = truncate_utf8(base_prefix, max_base_bytes).rstrip().rstrip(".") or "_"
    return f"{base_prefix} {suffix}"


def safe_join(base: Path, *parts: str) -> Path:
    candidate = base.joinpath(*parts)
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"path escapes base directory: {candidate}") from exc
    return candidate


def validate_relative_torrent_path(path_value: str) -> PurePosixPath:
    if not isinstance(path_value, str) or not path_value:
        raise ValueError("empty torrent file path")
    if "\x00" in path_value:
        raise ValueError("torrent file path contains NUL byte")
    p = PurePosixPath(path_value)
    if p.is_absolute() or any(part in {"", ".", ".."} for part in p.parts):
        raise ValueError(f"unsafe torrent file path: {path_value!r}")
    return p


@dataclass(frozen=True)
class TorrentFile:
    file_id: int
    path: str
    length: int
    order: int  # zero-based order from TorrServer FileStats; used by Elementum oindex


@dataclass(frozen=True)
class TorrentSnapshot:
    hash: str
    title: str
    category: str
    category_explicit: bool
    metadata: dict[str, Any]
    files: tuple[TorrentFile, ...]
    magnet: str | None = None
    quality: str | None = None
    quality_label: str | None = None


class TorrServerError(RuntimeError):
    pass


class TorrServerNotFound(TorrServerError):
    pass


class TorrServerClient:
    def __init__(self, base_url: str, timeout: float, remove_timeout: float):
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("torrserver.url must start with http:// or https://")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.remove_timeout = remove_timeout

    def _post(self, payload: dict[str, Any], timeout: float | None = None) -> Any:
        url = f"{self.base_url}/torrents"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "torr2strm/1.3.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout if timeout is None else timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            if exc.code == 404 and payload.get("action") == "get":
                raise TorrServerNotFound(str(payload.get("hash", ""))) from exc
            raise TorrServerError(f"HTTP {exc.code} {url}: {text[:500]}") from exc
        except urllib.error.URLError as exc:
            raise TorrServerError(f"cannot connect to {url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise TorrServerError(f"timeout talking to {url}") from exc
        except json.JSONDecodeError as exc:
            raise TorrServerError(f"invalid JSON from {url}: {exc}") from exc

    def list_torrents(self) -> list[dict[str, Any]]:
        data = self._post({"action": "list"})
        if not isinstance(data, list):
            raise TorrServerError("TorrServer list response is not an array")
        return data

    def get_torrent(self, torrent_hash: str) -> dict[str, Any]:
        data = self._post({"action": "get", "hash": torrent_hash})
        if not isinstance(data, dict):
            raise TorrServerError(f"TorrServer get response is not an object for {torrent_hash}")
        return data

    def load_torrent_metadata(self, torrent_hash: str) -> None:
        """Ask TorrServer to load a DB-only torrent so FileStats become available.

        TorrServer exposes this side effect through /playlist?hash=...: when the
        torrent exists only in DB (stat=TorrentInDB), the playlist handler calls
        LoadTorrent() and waits for torrent metadata. No media payload is read by
        this request; it only obtains the torrent metainfo needed for FileStats.
        """
        query = urllib.parse.urlencode({"hash": torrent_hash})
        url = f"{self.base_url}/playlist?{query}"
        req = urllib.request.Request(
            url, method="GET",
            headers={"Accept": "audio/x-mpegurl,text/plain,*/*", "User-Agent": "torr2strm/1.3.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read(1024)
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            raise TorrServerError(f"metadata load HTTP {exc.code} {url}: {text[:500]}") from exc
        except urllib.error.URLError as exc:
            raise TorrServerError(f"cannot connect to TorrServer metadata endpoint {url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise TorrServerError(f"timeout loading torrent metadata for {torrent_hash}") from exc

    def get_ffprobe(self, torrent_hash: str, file_id: int, timeout: float | None = None) -> dict[str, Any]:
        quoted_hash = urllib.parse.quote(torrent_hash, safe="")
        url = f"{self.base_url}/ffp/{quoted_hash}/{file_id}"
        req = urllib.request.Request(
            url, method="GET",
            headers={"Accept": "application/json", "User-Agent": "torr2strm/1.3.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout if timeout is None else timeout) as resp:
                raw = resp.read()
                if not raw:
                    raise TorrServerError(f"empty ffprobe response from {url}")
                decoded = json.loads(raw.decode("utf-8"))
                if not isinstance(decoded, dict):
                    raise TorrServerError(f"TorrServer ffprobe response is not an object for {torrent_hash}/{file_id}")
                return decoded
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            raise TorrServerError(f"ffprobe HTTP {exc.code} {url}: {text[:500]}") from exc
        except urllib.error.URLError as exc:
            raise TorrServerError(f"cannot connect to TorrServer ffprobe endpoint {url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise TorrServerError(f"timeout talking to TorrServer ffprobe endpoint {url}") from exc
        except json.JSONDecodeError as exc:
            raise TorrServerError(f"invalid JSON from TorrServer ffprobe endpoint {url}: {exc}") from exc

    def remove_torrent(self, torrent_hash: str) -> None:
        self._post({"action": "rem", "hash": torrent_hash}, timeout=self.remove_timeout)
        deadline = time.monotonic() + min(10.0, max(1.0, self.remove_timeout / 6.0))
        while time.monotonic() < deadline:
            if not self.torrent_present(torrent_hash):
                return
            time.sleep(0.5)
        raise TorrServerError(f"TorrServer still reports torrent {torrent_hash} after action=rem")

    def torrent_present(self, torrent_hash: str) -> bool:
        h = canonical_hash(torrent_hash)
        return any(
            isinstance(item, dict) and canonical_hash(str(get_field(item, "hash", default=""))) == h
            for item in self.list_torrents()
        )


def parse_snapshot(data: dict[str, Any], fallback_hash: str) -> TorrentSnapshot:
    torrent_hash = canonical_hash(str(get_field(data, "hash", default=fallback_hash)))
    title = get_field(data, "title", default="") or get_field(data, "name", default="")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"torrent {torrent_hash}: no title/name")
    raw_category = str(get_field(data, "category", default="") or "").strip().lower()
    category_explicit = bool(raw_category)
    category = raw_category if raw_category in {"movie", "tv"} else "_uncategorized"

    metadata = parse_json_object(get_field(data, "data", default=None))
    raw_files = get_field(data, "filestats", "files", default=None)
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError(f"torrent {torrent_hash}: FileStats absent/empty")

    files: list[TorrentFile] = []
    ids: set[int] = set()
    paths: set[str] = set()
    for order, item in enumerate(raw_files):
        if not isinstance(item, dict):
            raise ValueError(f"torrent {torrent_hash}: invalid FileStats item")
        try:
            file_id = int(get_field(item, "id", default=0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"torrent {torrent_hash}: invalid file id") from exc
        if file_id <= 0 or file_id in ids:
            raise ValueError(f"torrent {torrent_hash}: invalid/duplicate file id {file_id}")
        rel = validate_relative_torrent_path(str(get_field(item, "path", default="")))
        rel_str = rel.as_posix()
        if rel_str in paths:
            raise ValueError(f"torrent {torrent_hash}: duplicate file path {rel_str!r}")
        try:
            length = int(get_field(item, "length", "size", default=0) or 0)
        except (TypeError, ValueError):
            length = 0
        files.append(TorrentFile(file_id, rel_str, length, order))
        ids.add(file_id)
        paths.add(rel_str)
    # Keep a deterministic human/path order for layout logic, while preserving
    # the original zero-based FileStats order in TorrentFile.order for Elementum.
    files.sort(key=lambda x: x.path)
    magnet = extract_magnet(data)
    if magnet is None:
        magnet = extract_magnet(metadata)
    return TorrentSnapshot(torrent_hash, title.strip(), category, category_explicit, metadata, tuple(files), magnet=magnet)


def extract_list_embedded_torrent(item: dict[str, Any]) -> dict[str, Any] | None:
    data = parse_json_object(get_field(item, "data", default=None))
    embedded = data.get("TorrServer") if isinstance(data, dict) else None
    if not isinstance(embedded, dict):
        return None
    raw_files = get_field(embedded, "filestats", "files", default=None)
    return embedded if isinstance(raw_files, list) and raw_files else None


def is_video(path_value: str, extensions: set[str]) -> bool:
    return Path(path_value).suffix.lower() in extensions


def season_number(path_value: str, torrent_title: str) -> int | None:
    text = f"{path_value} {torrent_title}"
    patterns = [
        r"\bS(\d{1,2})E\d{1,3}\b",
        r"\bSeason\s*(\d{1,2})\b",
        r"\b(\d{1,2})x\d{1,3}\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


def bounded_strm_leaf(value: str, identity: str) -> str:
    """Return a readable STRM/NFO basename that stays below Linux NAME_MAX.

    Existing short names are preserved exactly. For a long UTF-8 name, keep
    the extension (when present), truncate on a character boundary, and add
    a short stable identity so distinct long source names cannot collapse to
    the same output filename. The returned value is the basename before the
    final ``.strm`` suffix.
    """
    clean = sanitize_component(value)
    max_bytes = 255 - len(".strm".encode("utf-8"))
    if len(clean.encode("utf-8")) <= max_bytes:
        return clean

    suffix = f" [{identity}]"
    ext = ""
    dot = clean.rfind(".")
    if dot > 0:
        ext = clean[dot:]
    stem = clean[:-len(ext)] if ext else clean
    budget = max_bytes - len(suffix.encode("utf-8")) - len(ext.encode("utf-8"))
    if budget < 1:
        raise ValueError("output filename suffix is too long for a Linux filename")
    stem = truncate_utf8(stem, budget).rstrip().rstrip(".") or "_"
    return f"{stem}{suffix}{ext}"


def atomic_write_text(path: Path, content: str, mode: int = 0o666) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A long final filename cannot be reused as the tempfile prefix: the
    # additional random suffix can push the temporary basename past NAME_MAX.
    fd, tmp_name = tempfile.mkstemp(prefix=".torr2strm-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, mode)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def load_manifest(path: Path, root: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": MANIFEST_VERSION, "root": str(root), "torrents": {}}
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or int(data.get("version", 0)) != MANIFEST_VERSION:
        raise RuntimeError(f"unsupported manifest at {path}; clean the old state before using v{VERSION}")
    recorded_root = str(data.get("root", ""))
    if recorded_root != str(root):
        raise RuntimeError(f"manifest root mismatch: recorded={recorded_root!r} configured={str(root)!r}")
    if not isinstance(data.get("torrents"), dict):
        raise RuntimeError("manifest.torrents must be an object")
    return data


def write_manifest_if_changed(path: Path, data: dict[str, Any]) -> bool:
    encoded = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == encoded:
        return False
    atomic_write_text(path, encoded)
    return True


@dataclass(frozen=True)
class JacRedMatch:
    result: dict[str, Any]
    ffprobe: dict[str, Any] | None
    magnet: str | None
    category: str | None
    query: str


def category_from_jacred_result(result: dict[str, Any]) -> str | None:
    """Map an exact JacRed/Prowlarr result to movie/tv/_uncategorized without guessing by title."""
    values: list[Any] = []
    for key in ("category", "categories", "cat", "Category"):
        if key in result:
            values.append(result[key])
    info = result.get("info") if isinstance(result.get("info"), dict) else {}
    for key in ("category", "categories", "cat", "type", "contentType"):
        if key in info:
            values.append(info[key])
    for key in ("type", "contentType"):
        if key in result:
            values.append(result[key])

    flat: list[Any] = []
    for value in values:
        if isinstance(value, (list, tuple, set)):
            flat.extend(value)
        else:
            flat.append(value)

    saw_anime = False
    saw_movie = False
    saw_tv = False
    for value in flat:
        text = str(value or "").strip().lower()
        if not text:
            continue
        if "anime" in text:
            saw_anime = True
            continue
        if text in {"movie", "movies", "film", "films"}:
            saw_movie = True
            continue
        if text in {"tv", "series", "show", "episode", "episodes", "tvshow"}:
            saw_tv = True
            continue
        for token in re.split(r"[\s,;:/|]+", text):
            if token.isdigit():
                number = int(token)
                if 2060 <= number < 3000 or 2000 <= number < 2100:
                    saw_movie = True
                elif number == 5060:
                    saw_anime = True
                elif 5000 <= number < 6000:
                    saw_tv = True
        if re.search(r"\b5000\b|\btv[-_ ]?show\b|\bseries\b", text):
            saw_tv = True
        if re.search(r"\b2000\b|\bmovie[s]?\b", text):
            saw_movie = True

    if saw_anime:
        return "_uncategorized"
    if saw_movie and not saw_tv:
        return "movie"
    if saw_tv and not saw_movie:
        return "tv"
    return None


def provider_ids_from_mapping(mapping: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(mapping, dict):
        return {}
    result = provider_ids(mapping, str(mapping.get("title") or ""))
    info = mapping.get("info")
    if isinstance(info, dict):
        for kind, value in provider_ids(info, str(info.get("title") or "")).items():
            result.setdefault(kind, value)
    return result


class JacRedClient:
    """Optional metadata provider using JacRed's Prowlarr Search Feed."""

    def __init__(self, cfg: dict[str, Any]):
        self.base_url = str(cfg.get("url", "")).strip().rstrip("/")
        self.api_key = str(cfg.get("api_key", "")).strip()
        self.indexer_id = int(cfg.get("indexer_id", 0))
        self.limit = int(cfg.get("limit", 100))
        self.timeout_sec = float(cfg.get("timeout_sec", 10))
        self.retries = int(cfg.get("retries", 0))
        self._cache: dict[tuple[str, str, int, int], list[dict[str, Any]]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def _get_json(self, query: str, search_type: str) -> list[dict[str, Any]]:
        key = (query, search_type, self.indexer_id, self.limit)
        if key in self._cache:
            return self._cache[key]
        params: list[tuple[str, str]] = [
            ("query", query),
            ("type", search_type),
            ("limit", str(self.limit)),
        ]
        if self.indexer_id > 0:
            params.append(("indexerIds", str(self.indexer_id)))
        url = f"{self.base_url}/api/v1/search?{urllib.parse.urlencode(params)}"
        headers = {"Accept": "application/json", "User-Agent": f"torr2strm/{VERSION}"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 2):
            req = urllib.request.Request(url, method="GET", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                    raw = resp.read()
                data = json.loads(raw.decode("utf-8")) if raw else []
                if not isinstance(data, list):
                    raise RuntimeError("JacRed /api/v1/search response is not an array")
                results = [item for item in data if isinstance(item, dict)]
                self._cache[key] = results
                LOG.info("JACRED_SEARCH query=%r type=%s results=%d", query, search_type, len(results))
                return results
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")
                last_error = RuntimeError(f"HTTP {exc.code}: {text[:300]}")
            except urllib.error.URLError as exc:
                last_error = RuntimeError(str(exc.reason))
            except TimeoutError as exc:
                last_error = RuntimeError(str(exc))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                last_error = RuntimeError(f"invalid JSON: {exc}")
            except Exception as exc:
                last_error = exc
            if attempt <= self.retries:
                time.sleep(0.5)
        LOG.warning("JACRED_SEARCH_FAILED query=%r type=%s error=%s", query, search_type, last_error)
        self._cache[key] = []
        return []

    def _candidate_queries(self, torrent: TorrentSnapshot) -> list[str]:
        raw_candidates: list[str] = []
        if torrent.category == "tv":
            raw_candidates.append(extract_tv_series_name(torrent.title, torrent.files))
        else:
            raw_candidates.append(torrent.title)
        for key in ("name", "originalname", "seriesTitle", "seriesName", "title", "originaltitle", "originalTitle"):
            value = get_field(torrent.metadata, key, default=None)
            if isinstance(value, str) and value.strip():
                raw_candidates.append(value)
        for value in list(raw_candidates):
            if " / " in value:
                raw_candidates.extend(part.strip() for part in value.split(" / "))
            if " | " in value:
                raw_candidates.extend(part.strip() for part in value.split(" | ")[:2])
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in raw_candidates:
            text = html.unescape(str(value)).strip()
            text = re.sub(r"\s+", " ", text)
            text = text.strip(" !|/\\-_.,")
            if not text or text in seen:
                continue
            seen.add(text)
            cleaned.append(text)
        return cleaned[:6]

    def find_match(self, torrent: TorrentSnapshot) -> JacRedMatch | None:
        if not self.enabled:
            return None
        search_type = "tvsearch" if torrent.category == "tv" else "movie" if torrent.category == "movie" else "search"
        target = canonical_hash(torrent.hash)
        exact: list[tuple[dict[str, Any], str]] = []
        for query in self._candidate_queries(torrent):
            for result in self._get_json(query, search_type):
                result_hash = canonical_hash(str(result.get("infoHash") or result.get("guid") or ""))
                if not result_hash:
                    result_hash = extract_btih(result.get("magnetUrl") or result.get("downloadUrl")) or ""
                if result_hash == target:
                    exact.append((result, query))

        if not exact:
            LOG.info("JACRED_MISS hash=%s", target)
            return None

        # Prefer one exact result that has a real ffprobe and a magnet. Keep all derived values from the same result.
        ranked = sorted(
            exact,
            key=lambda pair: (
                usable_ffprobe(pair[0].get("ffprobe")) is not None,
                bool(extract_magnet(pair[0].get("magnetUrl") or pair[0].get("downloadUrl"))),
                category_from_jacred_result(pair[0]) is not None,
            ),
            reverse=True,
        )
        result, query = ranked[0]
        payload = usable_ffprobe(result.get("ffprobe"))
        magnet = extract_magnet(result.get("magnetUrl") or result.get("downloadUrl"))
        category = category_from_jacred_result(result)
        if payload is None:
            LOG.warning("JACRED_MATCH_NO_FFPROBE hash=%s query=%r", target, query)
        LOG.info(
            "JACRED_MATCH hash=%s query=%r streams=%s category=%s magnet=%s",
            target, query, len(payload["streams"]) if payload else 0, category or "unknown", bool(magnet),
        )
        return JacRedMatch(result=result, ffprobe=payload, magnet=magnet, category=category, query=query)


class MediaInfoResolver:
    """Resolve media info once and share it between all output trees."""

    def __init__(self, cfg: dict[str, Any], client: TorrServerClient, output_manifests: list[tuple[Path, dict[str, Any]]]):
        self.cfg = cfg
        self.client = client
        self.timeout_sec = float(cfg["quality"]["timeout_sec"])
        self.retries = int(cfg["quality"]["retries"])
        self.extensions = cfg["sync"]["video_extensions"]
        self.output_manifests = output_manifests
        self.stats = {"torrserver_ffprobe": 0, "nfo_reused": 0}

    @staticmethod
    def _video_files(torrent: TorrentSnapshot, extensions: set[str]) -> list[TorrentFile]:
        files = [f for f in torrent.files if is_video(f.path, extensions)]
        files.sort(key=lambda f: (-f.length, f.path))
        return files

    @staticmethod
    def _read_nfo_root(path: Path) -> Any:
        return _nfo_xml_and_urls(path.read_text(encoding="utf-8"))[0]

    @staticmethod
    def _nfo_is_usable(path: Path) -> bool:
        if not path.is_file():
            return False
        try:
            root = MediaInfoResolver._read_nfo_root(path)
        except Exception:
            return False
        streams = root.find("./fileinfo/streamdetails")
        if streams is None:
            return False
        for node in streams.findall("video"):
            try:
                width = int((node.findtext("width") or "0").strip())
                height = int((node.findtext("height") or "0").strip())
            except ValueError:
                continue
            if width > 0 and height > 0 and (node.findtext("codec") or "").strip():
                return True
        return False

    @staticmethod
    def _quality_details_from_nfo(path: Path) -> tuple[str, str | None] | None:
        if not MediaInfoResolver._nfo_is_usable(path):
            return None
        try:
            root = MediaInfoResolver._read_nfo_root(path)
            node = root.find("./fileinfo/streamdetails/video")
            if node is None:
                return None
            width = int((node.findtext("width") or "0").strip())
            height = int((node.findtext("height") or "0").strip())
            if width <= 0 or height <= 0:
                return None
            stream: dict[str, Any] = {}
            hdr = str(node.findtext("hdrtype") or "").lower()
            if "dolby" in hdr or hdr in {"dv", "dolbyvision"}:
                stream["dv_profile"] = 1
            elif hdr:
                stream["color_transfer"] = "smpte2084"
            if (node.findtext("scantype") or "").lower() == "interlaced":
                stream["field_order"] = "tt"
            return quality_from_dimensions(width, height), quality_label_from_dimensions(width, height, stream)
        except Exception:
            return None

    @staticmethod
    def _quality_from_nfo(path: Path) -> str | None:
        details = MediaInfoResolver._quality_details_from_nfo(path)
        return details[0] if details else None

    def _cached_nfo(self, torrent: TorrentSnapshot, file: TorrentFile) -> tuple[Path, str] | None:
        """Find a valid existing NFO for this exact source file in any enabled output manifest."""
        for root, manifest in self.output_manifests:
            record = manifest.get("torrents", {}).get(torrent.hash) if isinstance(manifest.get("torrents"), dict) else None
            if not isinstance(record, dict):
                continue
            files = record.get("files") if isinstance(record.get("files"), dict) else {}
            for file_record in files.values():
                if not isinstance(file_record, dict) or file_record.get("source_path") != file.path:
                    continue
                rel = file_record.get("nfo")
                if not isinstance(rel, str) or not rel:
                    continue
                path = safe_join(root, *PurePosixPath(rel).parts)
                if self._nfo_is_usable(path):
                    content = path.read_text(encoding="utf-8")
                    content, migrated = normalize_cached_nfo(content)
                    if migrated:
                        LOG.info("NFO_MIGRATE path=%s from=legacy to=v%d", path, NFO_FORMAT_VERSION)
                    return path, content
        return None

    def _probe_torrserver(self, torrent: TorrentSnapshot, file: TorrentFile) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 2):
            try:
                payload = self.client.get_ffprobe(torrent.hash, file.file_id, timeout=self.timeout_sec)
                normalized = usable_ffprobe(payload)
                if normalized is None:
                    raise RuntimeError("TorrServer ffprobe returned no usable video stream")
                self.stats["torrserver_ffprobe"] += 1
                stream = ffprobe_video_stream(normalized)
                quality = quality_from_dimensions(int(stream.get("width") or 0), int(stream.get("height") or 0))
                LOG.info(
                    "MEDIAINFO hash=%s file=%s dimensions=%sx%s quality=%s via=torrserver",
                    torrent.hash, file.path, stream.get("width"), stream.get("height"), quality,
                )
                return normalized
            except Exception as exc:
                last_error = exc
                if attempt <= self.retries:
                    LOG.warning(
                        "FFPROBE_RETRY hash=%s file=%s attempt=%d/%d error=%s",
                        torrent.hash, file.path, attempt + 1, self.retries + 1, exc,
                    )
                    time.sleep(0.5)
        raise RuntimeError(f"ffprobe failed for {torrent.hash}/{file.file_id}: {last_error}")

    def prepare(
        self,
        torrent: TorrentSnapshot,
        match: JacRedMatch | None,
        required_file_ids: set[int],
    ) -> tuple[str, str | None, dict[int, dict[str, Any]], dict[int, str], dict[int, str]]:
        """Resolve one torrent-level quality and per-file ffprobe/NFO data."""
        candidates = self._video_files(torrent, self.extensions)
        if not candidates:
            raise RuntimeError(f"torrent {torrent.hash}: no video files")
        primary = candidates[0]
        required = [f for f in candidates if f.file_id in required_file_ids]
        if primary.file_id not in required_file_ids:
            required.insert(0, primary)

        cached: dict[int, tuple[Path, str]] = {}
        for file in required:
            found = self._cached_nfo(torrent, file)
            if found:
                cached[file.file_id] = found

        probes: dict[int, dict[str, Any]] = {}
        sources: dict[int, str] = {}
        cached_contents: dict[int, str] = {}
        for file_id, (_, text) in cached.items():
            cached_contents[file_id] = text
            sources[file_id] = "nfo"
            self.stats["nfo_reused"] += 1

        # The primary file decides the torrent-level root/label. Preserve each
        # additional per-file probe below solely to keep its own NFO accurate.
        root_quality: str | None = None
        quality_label: str | None = None
        attempted: set[int] = set()
        primary_cached = cached.get(primary.file_id)
        if primary_cached:
            details = self._quality_details_from_nfo(primary_cached[0])
            if details:
                root_quality, quality_label = details
                LOG.info("QUALITY_FROM_NFO hash=%s root=%s label=%s path=%s", torrent.hash, root_quality, quality_label, primary_cached[0])

        if quality_label is None and primary.file_id not in cached:
            attempted.add(primary.file_id)
            primary_payload: dict[str, Any] | None = None
            if match is not None and match.ffprobe is not None and usable_ffprobe(match.ffprobe) is not None:
                primary_payload = usable_ffprobe(match.ffprobe)
                sources[primary.file_id] = "jacred"
                LOG.info("MEDIAINFO hash=%s file=%s via=jacred", torrent.hash, primary.path)
            else:
                try:
                    primary_payload = self._probe_torrserver(torrent, primary)
                    sources[primary.file_id] = "torrserver"
                except Exception as exc:
                    LOG.warning("FFPROBE_UNAVAILABLE hash=%s file=%s role=primary error=%s", torrent.hash, primary.path, exc)
                    sources[primary.file_id] = "unavailable"
            if primary_payload is not None:
                probes[primary.file_id] = primary_payload
                quality_label = quality_label_from_probe(primary_payload)
                try:
                    root_quality = media_quality(primary_payload)
                except Exception:
                    root_quality = None
                if quality_label:
                    LOG.info("QUALITY hash=%s root=%s label=%s via=%s", torrent.hash, root_quality, quality_label, sources[primary.file_id])

        # Existing per-file probing remains in place: each NFO should describe
        # the media item next to it, not copy technical stream details from the
        # primary torrent file.
        for file in required:
            if file.file_id in cached or file.file_id in probes or file.file_id in attempted:
                continue
            attempted.add(file.file_id)
            try:
                payload = self._probe_torrserver(torrent, file)
                probes[file.file_id] = payload
                sources[file.file_id] = "torrserver"
            except Exception as exc:
                sources[file.file_id] = "unavailable"
                LOG.warning("FFPROBE_UNAVAILABLE hash=%s file=%s role=per-file-nfo error=%s", torrent.hash, file.path, exc)

        if quality_label is None:
            quality_label = quality_label_from_metadata(torrent.metadata)
            if quality_label:
                LOG.info("QUALITY_FALLBACK hash=%s label=%s via=structured_metadata", torrent.hash, quality_label)
        if quality_label is None and match is not None:
            quality_label = quality_label_from_metadata(match.result)
            if quality_label:
                LOG.info("QUALITY_FALLBACK hash=%s label=%s via=exact_jacred_structured_metadata", torrent.hash, quality_label)
        if quality_label is None:
            quality_label = quality_label_from_text(torrent.title)
            if quality_label:
                LOG.info("QUALITY_FALLBACK hash=%s label=%s via=release_title", torrent.hash, quality_label)

        if root_quality not in {"4K", "1080p"}:
            root_quality = quality_root_from_label(quality_label)
        if root_quality not in {"4K", "1080p"}:
            root_quality = "1080p"
        LOG.info(
            "MEDIAINFO_PLAN hash=%s required_files=%d cached_nfo=%d probes=%d root=%s label=%s",
            torrent.hash, len(required), len(cached), len(probes), root_quality, quality_label or "unknown",
        )
        return root_quality, quality_label, probes, sources, cached_contents


@dataclass(frozen=True)
class OutputSpec:
    name: str
    root: Path
    manifest_rel: PurePosixPath
    enabled: bool
    playback: str
    remove_on_missing: bool
    max_removals: int


class OutputRunner:
    def __init__(self, spec: OutputSpec, cfg: dict[str, Any], client: TorrServerClient, *, dry_run: bool):
        self.spec = spec
        self.cfg = cfg
        self.client = client
        self.dry_run = dry_run
        self.root = spec.root
        self.manifest_path = self.root / spec.manifest_rel
        self.tv_unmatched_season = cfg["sync"]["tv_unmatched_season"]
        self.removals_this_run = 0
        self.removed_hashes: set[str] = set()
        self.kodi_dirname_by_hash: dict[str, str] = {}
        self.kodi_display_title_by_hash: dict[str, str] = {}
        self.kodi_identity_by_hash: dict[str, str | None] = {}
        self.kodi_hash_by_torrent: dict[str, str] = {}

    def _root_marker(self) -> Path:
        return self.manifest_path.parent / "root.marker"

    def _check_or_create_marker(self) -> None:
        marker = self._root_marker()
        if marker.exists():
            text = marker.read_text(encoding="utf-8", errors="replace")
            match = re.search(r"^root=(.*)$", text, flags=re.MULTILINE)
            if match and match.group(1).strip() != str(self.root):
                raise RuntimeError(
                    f"root marker mismatch: recorded={match.group(1).strip()!r} configured={str(self.root)!r} output={self.spec.name}"
                )
            if not match:
                raise RuntimeError(f"root marker malformed: {marker}")
            return
        if self.manifest_path.exists():
            raise RuntimeError(f"state marker is missing: {marker}; refusing sync/deletion for output {self.spec.name}")
        if not self.dry_run:
            atomic_write_text(
                marker,
                "torr2strm-root-marker-v3\n"
                f"created={int(time.time())}\n"
                f"root={self.root}\n"
                f"output={self.spec.name}\n",
            )

    def load_manifest(self) -> dict[str, Any]:
        self._check_or_create_marker()
        return load_manifest(self.manifest_path, self.root)

    def prepare_root(self) -> None:
        if not self.root.exists() and not self.dry_run:
            self.root.mkdir(parents=True, exist_ok=True)
            os.chmod(self.root, 0o777)
        elif self.root.exists() and not self.root.is_dir():
            raise RuntimeError(f"output root is not a directory: {self.root}")
        if not self.dry_run:
            self.root.mkdir(parents=True, exist_ok=True)
            os.chmod(self.root, 0o777)
            (self.manifest_path.parent).mkdir(parents=True, exist_ok=True)
            os.chmod(self.manifest_path.parent, 0o777)

    def _assert_no_symlink(self, path: Path) -> None:
        try:
            rel = path.relative_to(self.root)
        except ValueError as exc:
            raise RuntimeError(f"managed path escaped output root: {path}") from exc
        cur = self.root
        for part in rel.parts:
            cur = cur / part
            if cur.is_symlink():
                raise RuntimeError(f"refusing symlink in managed path: {cur}")

    def _mkdir(self, path: Path) -> None:
        self._assert_no_symlink(path)
        if path.exists():
            if not path.is_dir():
                raise RuntimeError(f"expected directory, found non-directory: {path}")
            if not self.dry_run:
                os.chmod(path, 0o777)
            return
        if self.dry_run:
            LOG.info("DRY-RUN CREATE-DIR output=%s path=%s", self.spec.name, path)
            return
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o777)
        LOG.info("CREATE-DIR output=%s path=%s", self.spec.name, path)

    def _delete_tree(self, path: Path, h: str, reason: str) -> None:
        self._assert_no_symlink(path)
        if not path.exists():
            return
        if not path.is_dir():
            raise RuntimeError(f"managed torrent path is not a directory: {path}")
        if self.dry_run:
            LOG.info("DRY-RUN REMOVE-TREE output=%s hash=%s path=%s reason=%s", self.spec.name, h, path, reason)
            return
        shutil.rmtree(path)
        LOG.warning("REMOVE-TREE output=%s hash=%s path=%s reason=%s", self.spec.name, h, path, reason)

    def _existing_old_dir(self, old: dict[str, Any] | None) -> Path | None:
        rel = old.get("directory") if isinstance(old, dict) else None
        if not isinstance(rel, str) or not rel:
            return None
        return safe_join(self.root, *PurePosixPath(rel).parts)

    def _record_dir(self, snap: TorrentSnapshot) -> Path:
        if snap.quality not in {"4K", "1080p"}:
            raise RuntimeError(f"torrent {snap.hash}: missing quality")
        if self.spec.name == "kodi":
            name = self.kodi_dirname_by_hash.get(snap.hash, sanitize_component(snap.title))
        else:
            # Jellyfin's existing per-torrent naming and folder behavior is unchanged.
            name = build_torrent_dir_name(snap.title, snap.metadata, snap.hash, snap.category, snap.files)
        return safe_join(self.root, snap.category, snap.quality, name)

    def _handle_missing_managed(self, snap: TorrentSnapshot, old: dict[str, Any] | None) -> bool:
        if self.spec.playback != "torrserver" or not self.spec.remove_on_missing:
            return False
        old_files = old.get("files", {}) if isinstance(old, dict) else {}
        if not isinstance(old_files, dict) or not old_files:
            return False
        missing = []
        for rel_path in old_files:
            managed = safe_join(self.root, *PurePosixPath(rel_path).parts)
            if not managed.is_file():
                missing.append(rel_path)
        if not missing:
            return False
        LOG.warning("STRM_MISSING output=%s hash=%s count=%d first=%s", self.spec.name, snap.hash, len(missing), missing[0])
        if self.dry_run:
            LOG.info("DRY-RUN REMOVE output=%s hash=%s reason=strm_deleted_by_user", self.spec.name, snap.hash)
            self.removed_hashes.add(snap.hash)
            return True
        if self.removals_this_run >= self.spec.max_removals:
            LOG.error(
                "ABORT-REMOVE output=%s hash=%s reason=max_removals_per_run_reached limit=%d",
                self.spec.name, snap.hash, self.spec.max_removals,
            )
            return False
        try:
            self.client.remove_torrent(snap.hash)
        except TorrServerError as exc:
            LOG.exception("ERROR output=%s hash=%s operation=rem reason=strm_deleted_by_user error=%s", self.spec.name, snap.hash, exc)
            return False
        self.removals_this_run += 1
        self.removed_hashes.add(snap.hash)
        LOG.warning("REMOVE output=%s hash=%s operation=rem reason=strm_deleted_by_user", self.spec.name, snap.hash)
        old_dir = self._existing_old_dir(old)
        if old_dir is not None:
            self._delete_tree(old_dir, snap.hash, "source_removed_by_jellyfin_reverse_delete")
        return True

    @staticmethod
    def _primary_video(snap: TorrentSnapshot, extensions: set[str]) -> TorrentFile:
        candidates = [f for f in snap.files if is_video(f.path, extensions)]
        if not candidates:
            raise RuntimeError(f"torrent {snap.hash}: no video files")
        return sorted(candidates, key=lambda f: (-f.length, f.path))[0]

    def _elementum_url(self, snap: TorrentSnapshot, file: TorrentFile | None = None) -> str:
        magnet = snap.magnet or constructed_magnet(snap.hash, snap.title)
        if snap.magnet:
            source = "source"
        else:
            source = "constructed"
        encoded = urllib.parse.quote(magnet, safe="")
        url = f"plugin://plugin.video.elementum/play?uri={encoded}"
        if file is not None:
            url += f"&oindex={file.order}"
        LOG.info("KODI_URI hash=%s mode=%s magnet=%s oindex=%s", snap.hash, self.spec.name, source, file.order if file is not None else "torrent")
        return url

    def _write_strm(self, path: Path, content: str, h: str, source_path: str, rel_path: str) -> None:
        if path.exists():
            if path.is_dir():
                LOG.error("ERROR output=%s hash=%s destination=%s reason=destination_is_directory", self.spec.name, h, rel_path)
                return
            try:
                old = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                old = None
            if old == content:
                if not self.dry_run:
                    os.chmod(path, 0o666)
                return
            if self.dry_run:
                LOG.info("DRY-RUN UPDATE output=%s hash=%s source=%s destination=%s reason=url_changed", self.spec.name, h, source_path, rel_path)
                return
            atomic_write_text(path, content)
            LOG.info("UPDATE output=%s hash=%s source=%s destination=%s reason=url_changed", self.spec.name, h, source_path, rel_path)
            return
        if self.dry_run:
            LOG.info("DRY-RUN CREATE output=%s hash=%s source=%s destination=%s reason=missing", self.spec.name, h, source_path, rel_path)
            return
        atomic_write_text(path, content)
        LOG.info("CREATE output=%s hash=%s source=%s destination=%s reason=missing", self.spec.name, h, source_path, rel_path)

    def _write_nfo(self, path: Path, content: str, h: str, source_path: str, rel_path: str) -> None:
        if path.exists():
            if path.is_dir():
                LOG.error("ERROR_NFO output=%s hash=%s destination=%s reason=destination_is_directory", self.spec.name, h, rel_path)
                return
            try:
                old = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                old = None
            if old == content:
                if not self.dry_run:
                    os.chmod(path, 0o666)
                return
            if self.dry_run:
                LOG.info("DRY-RUN NFO_UPDATE output=%s hash=%s source=%s destination=%s", self.spec.name, h, source_path, rel_path)
                return
            atomic_write_text(path, content)
            LOG.info("NFO_UPDATE output=%s hash=%s source=%s destination=%s", self.spec.name, h, source_path, rel_path)
            return
        if self.dry_run:
            LOG.info("DRY-RUN NFO_CREATE output=%s hash=%s source=%s destination=%s", self.spec.name, h, source_path, rel_path)
            return
        atomic_write_text(path, content)
        LOG.info("NFO_CREATE output=%s hash=%s source=%s destination=%s", self.spec.name, h, source_path, rel_path)

    @staticmethod
    def _hdr_type(stream: dict[str, Any]) -> str | None:
        dv_profile = stream.get("dv_profile")
        if dv_profile not in (None, "", 0):
            return "dolbyvision"
        transfer = str(stream_tag_value(stream, "color_transfer", "transfer_characteristics", "colorTransfer") or "").lower()
        if transfer in {"arib-std-b67", "arib_std_b67"}:
            return "hlg"
        if transfer in {"smpte2084", "pq"}:
            return "hdr10"
        return None

    def _nfo_content(self, snap: TorrentSnapshot, file: TorrentFile, payload: dict[str, Any] | None, source: str) -> str:
        """Create title-free NFO metadata; each STRM's stream details come from its own source file."""
        import xml.etree.ElementTree as ET
        root_tag = "episodedetails" if snap.category == "tv" else "movie"
        root = ET.Element(root_tag)
        ids = provider_ids_for_snapshot(snap)

        # Never emit title/name fields: Kodi may overwrite the localized scraper
        # title with NFO titles after scraping, and Jellyfin should localize by ID.
        if snap.category == "tv":
            season, episode = source_episode_coordinates(file.path, snap.title)
            if season is not None:
                add_xml_text(root, "season", season)
            if episode is not None:
                add_xml_text(root, "episode", episode)

        default_type = "tmdb" if "tmdb" in ids else "imdb" if "imdb" in ids else "tvdb" if "tvdb" in ids else next(iter(ids), None)
        for kind, value in ids.items():
            if kind in {"tmdb", "imdb", "tvdb", "tvmaze", "trakt", "kinopoisk", "mal", "anidb", "anilist", "douban", "wikidata"}:
                if kind in {"tmdb", "imdb", "tvdb"}:
                    add_xml_text(root, f"{kind}id", value)
                attrs = {"type": kind}
                if kind == default_type:
                    attrs["default"] = "true"
                node = ET.SubElement(root, "uniqueid", attrs)
                node.text = value

        normalized = usable_ffprobe(payload) if payload is not None else None
        if normalized is not None:
            fileinfo = ET.SubElement(root, "fileinfo")
            details = ET.SubElement(fileinfo, "streamdetails")
            for stream in normalized["streams"]:
                if not isinstance(stream, dict):
                    continue
                stype = str(stream.get("codec_type", "")).lower()
                if stype == "video":
                    node = ET.SubElement(details, "video")
                    append_common_stream_fields(node, stream)
                    add_xml_text(node, "width", stream.get("width"))
                    add_xml_text(node, "height", stream.get("height"))
                    aspect, aspectratio = stream_aspect(stream)
                    add_xml_text(node, "aspect", aspect)
                    add_xml_text(node, "aspectratio", aspectratio)
                    add_xml_text(node, "framerate", stream_framerate(stream))
                    add_xml_text(node, "scantype", stream_scantype(stream))
                    add_xml_text(node, "bitdepth", stream.get("bits_per_raw_sample", stream.get("bits_per_sample")))
                    add_xml_text(node, "hdrtype", self._hdr_type(stream))
                    add_xml_text(node, "stereomode", stream_tag_value(stream, "stereo_mode"))
                elif stype == "audio":
                    node = ET.SubElement(details, "audio")
                    append_common_stream_fields(node, stream)
                    add_xml_text(node, "channels", stream.get("channels"))
                    add_xml_text(node, "samplingrate", stream.get("sample_rate"))
                elif stype == "subtitle":
                    node = ET.SubElement(details, "subtitle")
                    append_common_stream_fields(node, stream)

        # Combination NFO URLs apply to movies only; episodes use ordinary NFO.
        combination_url = _nfo_combination_url("movie", ids.get("tmdb")) if root_tag == "movie" else None
        return _finalize_nfo(
            root,
            source_comment=f"media_info_source={source}",
            combination_url=combination_url,
        )

    def _tvshow_nfo_content(
        self,
        snap: TorrentSnapshot,
        logical_identity: str | None = None,
        streamdetails_nfo: str | None = None,
    ) -> str:
        import xml.etree.ElementTree as ET
        root = ET.Element("tvshow")
        group_key = (snap.category, snap.quality or "1080p", logical_identity) if logical_identity else None
        ids = (
            self.kodi_series_canonical_ids.get(group_key, provider_ids_for_snapshot(snap))
            if group_key
            else provider_ids_for_snapshot(snap)
        )
        default_type = "tmdb" if "tmdb" in ids else "imdb" if "imdb" in ids else "tvdb" if "tvdb" in ids else next(iter(ids), None)
        for kind, value in ids.items():
            if kind in {"tmdb", "imdb", "tvdb", "tvmaze", "trakt", "kinopoisk", "mal", "anidb", "anilist", "douban", "wikidata"}:
                if kind in {"tmdb", "imdb", "tvdb"}:
                    add_xml_text(root, f"{kind}id", value)
                attrs = {"type": kind}
                if kind == default_type:
                    attrs["default"] = "true"
                node = ET.SubElement(root, "uniqueid", attrs)
                node.text = value
        if streamdetails_nfo:
            try:
                stream_root = _nfo_xml_and_urls(streamdetails_nfo)[0]
                fileinfo = stream_root.find("./fileinfo")
                if fileinfo is not None:
                    root.append(ET.fromstring(ET.tostring(fileinfo, encoding="unicode")))
            except Exception as exc:
                LOG.warning("TVSHOW_STREAMDETAILS_SKIP hash=%s error=%s", snap.hash, exc)
        return _finalize_nfo(
            root,
            source_comment="kind=tvshow",
            combination_url=_nfo_combination_url("tvshow", ids.get("tmdb")),
        )

    def _prepare_kodi_components(self, snapshots: dict[str, TorrentSnapshot]) -> None:
        """Choose canonical shared directory names and collision-safe short hashes for one sync."""
        self.kodi_dirname_by_hash: dict[str, str] = {}
        self.kodi_display_title_by_hash: dict[str, str] = {}
        self.kodi_identity_by_hash: dict[str, str | None] = {}
        self.kodi_series_canonical_ids: dict[tuple[str, str, str], dict[str, str]] = {}
        self.kodi_series_canonical_hash_by_identity: dict[tuple[str, str, str], str] = {}
        self.kodi_hash_by_torrent = {h: h[:8] for h in snapshots}
        by_prefix: dict[str, list[str]] = {}
        for h in snapshots:
            by_prefix.setdefault(h[:8], []).append(h)
        for hashes in by_prefix.values():
            if len(hashes) > 1:
                length = 9
                while length < 41 and len({h[:length] for h in hashes}) < len(hashes):
                    length += 1
                for h in hashes:
                    self.kodi_hash_by_torrent[h] = h[:length]

        grouped: dict[tuple[str, str, str], list[tuple[int, str, str | None, str]]] = {}
        fallback: dict[tuple[str, str, str], list[str]] = {}
        for h, snap in snapshots.items():
            quality_root = snap.quality if snap.quality in {"4K", "1080p"} else "1080p"
            identity = media_logical_identity(snap) if snap.category in {"tv", "movie"} else None
            self.kodi_identity_by_hash[h] = identity
            if identity:
                title, year, priority = explicit_display_title(snap, tv=snap.category == "tv")
                grouped.setdefault((snap.category, quality_root, identity), []).append((priority, title, year, h))
            else:
                # Unidentified TV/movie torrents and unsupported categories retain
                # their own torrent-title directory and original inner hierarchy.
                name = sanitize_component(snap.title)
                fallback.setdefault((snap.category, quality_root, name.casefold()), []).append(h)
                self.kodi_display_title_by_hash[h] = name

        # The same known logical ID always gets one canonical display name, even
        # when release titles and internal torrent folder names differ.
        base_components: dict[tuple[str, str, str], tuple[str, str | None]] = {}
        for key, candidates in grouped.items():
            candidates.sort(key=lambda item: (item[0], item[1].casefold(), item[2] or "", item[3]))
            _, title, year, _ = candidates[0]
            base_components[key] = (title, year)
            for _, _, _, h in candidates:
                self.kodi_display_title_by_hash[h] = title

        collisions: dict[tuple[str, str, str], set[str]] = {}
        for (category, quality_root, identity), (title, year) in base_components.items():
            component = clean_media_component(title, year)
            collisions.setdefault((category, quality_root, component.casefold()), set()).add(identity)
        for key, (title, year) in base_components.items():
            category, quality_root, identity = key
            component = clean_media_component(title, year)
            if len(collisions.get((category, quality_root, component.casefold()), set())) > 1:
                kind, value = identity.split(":", 1)
                component = sanitize_component(f"{component} [{kind}-{value}]")
            for _, _, _, h in grouped[key]:
                self.kodi_dirname_by_hash[h] = component
            # A shared tvshow.nfo must be stable across all releases. Store only
            # the canonical logical provider ID selected for this series rather
            # than whichever episode torrent happens to sync last.
            if category == "tv":
                canonical_hash = grouped[key][0][3]
                self.kodi_series_canonical_ids[key] = provider_ids_for_snapshot(snapshots[canonical_hash])
                self.kodi_series_canonical_hash_by_identity[key] = canonical_hash

        # Add a short hash to an unidentified release directory only if another
        # current torrent would otherwise claim the same directory.
        for key, hashes in fallback.items():
            if len(hashes) == 1:
                h = hashes[0]
                self.kodi_dirname_by_hash[h] = self.kodi_display_title_by_hash[h]
            else:
                for h in hashes:
                    self.kodi_dirname_by_hash[h] = sanitize_component(
                        f"{self.kodi_display_title_by_hash[h]} [{self.kodi_hash_by_torrent[h]}]"
                    )

    def _kodi_episode_basename(self, snap: TorrentSnapshot, file: TorrentFile, duplicate: bool = False) -> str:
        title = self.kodi_display_title_by_hash.get(snap.hash) or extract_tv_series_name(snap.title, snap.files)
        season, episode = source_episode_coordinates(file.path, snap.title)
        if episode is not None and season is not None:
            stem = f"{title} S{season:02d}E{episode:02d}"
        elif episode is not None:
            stem = f"{title} E{episode:02d}"
        else:
            stem = Path(file.path).stem or Path(file.path).name
        label = quality_marker(snap.quality_label)
        hash_label = self.kodi_hash_by_torrent.get(snap.hash, snap.hash[:8])
        return bounded_kodi_leaf(
            stem,
            snap.quality_label,
            hash_label,
            file_ordinal=(file.order + 1) if duplicate else None,
        )

    def _kodi_fallback_basename(self, snap: TorrentSnapshot, file: TorrentFile) -> str:
        stem = Path(file.path).stem or Path(file.path).name
        return bounded_kodi_leaf(
            stem,
            snap.quality_label,
            self.kodi_hash_by_torrent.get(snap.hash, snap.hash[:8]),
        )

    def _sync_kodi_torrent(
        self,
        snap: TorrentSnapshot,
        old: dict[str, Any] | None,
        media_payloads: dict[int, dict[str, Any]],
        media_sources: dict[int, str],
        cached_nfo: dict[int, str],
    ) -> dict[str, Any] | None:
        if snap.quality not in {"4K", "1080p"}:
            snap = replace(snap, quality="1080p")
        directory_name = self.kodi_dirname_by_hash.get(snap.hash, sanitize_component(snap.title))
        torrent_dir = safe_join(self.root, snap.category, snap.quality, directory_name)
        self._assert_no_symlink(torrent_dir.parent)
        self._assert_no_symlink(torrent_dir)
        rel_torrent_dir = torrent_dir.relative_to(self.root).as_posix()
        if torrent_dir.exists() and not torrent_dir.is_dir():
            LOG.error("ERROR output=kodi hash=%s destination=%s reason=destination_exists_as_file", snap.hash, torrent_dir)
            return None
        self._mkdir(torrent_dir)

        desired: dict[str, dict[str, Any]] = {}
        desired_dirs: set[str] = {rel_torrent_dir}
        video_files = [f for f in snap.files if is_video(f.path, self.cfg["sync"]["video_extensions"])]
        if not video_files:
            raise RuntimeError(f"torrent {snap.hash}: no video files")
        primary = sorted(video_files, key=lambda f: (-f.length, f.path))[0]
        identity = self.kodi_identity_by_hash.get(snap.hash)
        normalized_tv = snap.category == "tv" and identity is not None
        normalized_movie = snap.category == "movie" and identity is not None

        targets: list[tuple[TorrentFile, Path, str, str, bool]] = []
        if snap.category == "movie":
            # Preserve the existing Elementum movie model: one torrent-level link,
            # using the primary video's file-specific NFO data.
            base = self.kodi_display_title_by_hash.get(snap.hash) or nfo_movie_title(snap)
            short = self.kodi_hash_by_torrent.get(snap.hash, snap.hash[:8])
            leaf = bounded_kodi_leaf(base, snap.quality_label, short)
            targets.append((primary, torrent_dir, self._elementum_url(snap), leaf, False))
        else:
            duplicate_groups: dict[tuple[int | None, int | None], list[TorrentFile]] = {}
            if normalized_tv:
                for file in video_files:
                    coords = source_episode_coordinates(file.path, snap.title)
                    if coords[0] is not None and coords[1] is not None:
                        duplicate_groups.setdefault(coords, []).append(file)
            for file in video_files:
                if normalized_tv:
                    season, episode = source_episode_coordinates(file.path, snap.title)
                    if season is None:
                        season = self.tv_unmatched_season
                        LOG.warning("SEASON_FALLBACK output=kodi hash=%s file=%s season=%02d", snap.hash, file.path, season)
                    target_dir = safe_join(torrent_dir, f"Season {season:02d}")
                    coords = source_episode_coordinates(file.path, snap.title)
                    duplicate = (
                        coords[0] is not None
                        and coords[1] is not None
                        and len(duplicate_groups.get(coords, [])) > 1
                    )
                    leaf = self._kodi_episode_basename(snap, file, duplicate=duplicate)
                else:
                    # Unknown identity: retain the source torrent's internal hierarchy.
                    target_dir = safe_join(torrent_dir, *PurePosixPath(file.path).parent.parts)
                    leaf = self._kodi_fallback_basename(snap, file)
                playback = self._elementum_url(snap, file)
                targets.append((file, target_dir, playback, leaf, normalized_tv))

        basename_counts: dict[tuple[str, str], int] = {}
        for _, target_dir, _, leaf, _ in targets:
            key = (str(target_dir), leaf.casefold())
            basename_counts[key] = basename_counts.get(key, 0) + 1
        resolved_targets: list[tuple[TorrentFile, Path, str, str, bool]] = []
        for file, target_dir, url, leaf, is_normalized_tv in targets:
            if basename_counts[(str(target_dir), leaf.casefold())] > 1:
                # True duplicate episode coordinates already include an ordinal;
                # this branch handles real filename collisions such as equal stems.
                if "[file" not in leaf:
                    base = self.kodi_display_title_by_hash.get(snap.hash) if is_normalized_tv else Path(file.path).stem
                    if not base:
                        base = Path(file.path).stem or Path(file.path).name
                    leaf = bounded_kodi_leaf(
                        base,
                        snap.quality_label,
                        self.kodi_hash_by_torrent.get(snap.hash, snap.hash[:8]),
                        file_ordinal=file.order + 1,
                    )
            resolved_targets.append((file, target_dir, url, leaf, is_normalized_tv))
        targets = resolved_targets

        for file, target_dir, url, leaf, is_normalized_tv in targets:
            self._assert_no_symlink(target_dir)
            self._mkdir(target_dir)
            desired_dirs.add(target_dir.relative_to(self.root).as_posix())
            strm_path = safe_join(target_dir, leaf + ".strm")
            rel_strm = strm_path.relative_to(self.root).as_posix()
            nfo_path = strm_path.with_suffix(".nfo")
            rel_nfo = nfo_path.relative_to(self.root).as_posix()
            if rel_strm in desired:
                raise RuntimeError(f"duplicate planned Kodi destination {rel_strm!r} for torrent {snap.hash}")
            if strm_path.exists() and not (isinstance(old, dict) and rel_strm in old.get("files", {})):
                raise RuntimeError(f"unmanaged Kodi file already occupies destination {rel_strm!r}")

            payload = media_payloads.get(file.file_id)
            source = media_sources.get(file.file_id, "unavailable")
            if payload is not None:
                nfo_content = self._nfo_content(snap, file, payload, source)
            elif file.file_id in cached_nfo:
                nfo_content = cached_nfo[file.file_id]
                nfo_content, _ = normalize_cached_nfo(nfo_content)
                source = "nfo"
            elif nfo_path.is_file():
                # A current item NFO is already at the same deterministic path.
                old_text = nfo_path.read_text(encoding="utf-8", errors="replace")
                if nfo_format_version(old_text) == NFO_FORMAT_VERSION:
                    nfo_content = old_text
                else:
                    nfo_content = self._nfo_content(snap, file, None, source)
            else:
                nfo_content = self._nfo_content(snap, file, None, source)

            self._write_nfo(nfo_path, nfo_content, snap.hash, file.path, rel_nfo)
            self._write_strm(strm_path, url, snap.hash, file.path, rel_strm)
            desired[rel_strm] = {
                "source_path": file.path,
                "file_id": file.file_id,
                "file_order": file.order,
                "length": file.length,
                "url": url,
                "content_sha256": sha256_text(url + "\n"),
                "nfo": rel_nfo,
                "nfo_sha256": sha256_text(nfo_content),
                "media_info_source": source,
                "quality_root": snap.quality,
                "quality_label": snap.quality_label,
                "logical_identity": identity,
            }

        tvshow_nfo_rel: str | None = None
        if normalized_tv:
            tvshow_path = safe_join(torrent_dir, "tvshow.nfo")
            tvshow_nfo_rel = tvshow_path.relative_to(self.root).as_posix()
            self._write_nfo(tvshow_path, self._tvshow_nfo_content(snap), snap.hash, snap.title, tvshow_nfo_rel)

        ids = provider_ids(snap.metadata, snap.title)
        metadata_record = {f"{kind}id": value for kind, value in ids.items()}
        return {
            "hash": snap.hash,
            "title": snap.title,
            "category": snap.category,
            "quality": snap.quality,
            "quality_label": snap.quality_label,
            "metadata": metadata_record,
            "magnet": snap.magnet or constructed_magnet(snap.hash, snap.title),
            "directory": rel_torrent_dir,
            "tvshow_nfo": tvshow_nfo_rel,
            "mode": self.spec.name,
            "files": desired,
            "directories": sorted(desired_dirs),
        }

    def _sync_torrent(
        self,
        snap: TorrentSnapshot,
        old: dict[str, Any] | None,
        media_payloads: dict[int, dict[str, Any]],
        media_sources: dict[int, str],
        cached_nfo: dict[int, str],
    ) -> dict[str, Any] | None:
        if self.spec.name == "kodi":
            return self._sync_kodi_torrent(snap, old, media_payloads, media_sources, cached_nfo)
        torrent_dir = self._record_dir(snap)
        self._assert_no_symlink(torrent_dir.parent)
        self._assert_no_symlink(torrent_dir)
        rel_torrent_dir = torrent_dir.relative_to(self.root).as_posix()

        old_dir = self._existing_old_dir(old)
        if old_dir is not None and old_dir.as_posix() != torrent_dir.as_posix() and old_dir.exists() and not torrent_dir.exists():
            self._mkdir(torrent_dir.parent)
            if self.dry_run:
                LOG.info("DRY-RUN RENAME output=%s hash=%s from=%s to=%s reason=identity_changed", self.spec.name, snap.hash, old_dir.relative_to(self.root), rel_torrent_dir)
            else:
                os.replace(old_dir, torrent_dir)
                LOG.info("RENAME output=%s hash=%s from=%s to=%s reason=identity_changed", self.spec.name, snap.hash, old_dir.relative_to(self.root), rel_torrent_dir)
        elif old_dir is not None and old_dir.as_posix() != torrent_dir.as_posix() and old_dir.exists() and torrent_dir.exists():
            LOG.error("PATH_CONFLICT output=%s hash=%s old=%s new=%s", self.spec.name, snap.hash, old_dir, torrent_dir)
            return None

        if torrent_dir.exists() and not torrent_dir.is_dir():
            LOG.error("ERROR output=%s hash=%s destination=%s reason=destination_exists_as_file", self.spec.name, snap.hash, torrent_dir)
            return None
        if old is None and torrent_dir.exists():
            LOG.warning("PATH_CONFLICT output=%s hash=%s destination=%s reason=unmanaged_directory_exists", self.spec.name, snap.hash, torrent_dir)
            return None

        if self._handle_missing_managed(snap, old):
            return None

        self._mkdir(torrent_dir)
        desired: dict[str, dict[str, Any]] = {}
        desired_dirs: set[str] = {rel_torrent_dir}
        seen_destinations: set[str] = set()
        video_files = [f for f in snap.files if is_video(f.path, self.cfg["sync"]["video_extensions"])]
        if not video_files:
            raise RuntimeError(f"torrent {snap.hash}: no video files")
        primary = sorted(video_files, key=lambda f: (-f.length, f.path))[0]

        if self.spec.name == "kodi" and snap.category == "movie":
            targets: list[tuple[TorrentFile, Path, str]] = [(primary, torrent_dir, self._elementum_url(snap))]
        else:
            targets = []
            for file in video_files:
                rel_src = PurePosixPath(file.path)
                if snap.category == "tv":
                    season = season_number(file.path, snap.title)
                    if season is None:
                        season = self.tv_unmatched_season
                        LOG.warning("SEASON_FALLBACK output=%s hash=%s file=%s season=%02d", self.spec.name, snap.hash, file.path, season)
                    target_dir = safe_join(torrent_dir, f"Season {season:02d}")
                    playback_url = self._elementum_url(snap, file) if self.spec.name == "kodi" else f"{self.client.base_url}/play/{urllib.parse.quote(snap.hash, safe='')}/{file.file_id}"
                    targets.append((file, target_dir, playback_url))
                else:
                    target_dir = safe_join(torrent_dir, *rel_src.parent.parts)
                    playback_url = self._elementum_url(snap, None) if self.spec.name == "kodi" else f"{self.client.base_url}/play/{urllib.parse.quote(snap.hash, safe='')}/{file.file_id}"
                    targets.append((file, target_dir, playback_url))

        for file, target_dir, url in targets:
            self._mkdir(target_dir)
            desired_dirs.add(target_dir.relative_to(self.root).as_posix())
            if self.spec.name == "kodi" and snap.category == "movie":
                base_name = bounded_strm_leaf(nfo_movie_title(snap), snap.hash[:8])
                strm_path = safe_join(torrent_dir, base_name + ".strm")
            else:
                base_name = bounded_strm_leaf(Path(file.path).name, f"{snap.hash[:8]}-{file.file_id}")
                strm_path = safe_join(target_dir, base_name + ".strm")
            rel_strm = strm_path.relative_to(self.root).as_posix()
            nfo_path = strm_path.with_suffix(".nfo")
            rel_nfo = nfo_path.relative_to(self.root).as_posix()
            if rel_strm in seen_destinations:
                LOG.error("STRM_COLLISION output=%s hash=%s destination=%s source=%s", self.spec.name, snap.hash, rel_strm, file.path)
                continue
            seen_destinations.add(rel_strm)

            payload = media_payloads.get(file.file_id)
            source = media_sources.get(file.file_id, "nfo")
            nfo_content = None
            if payload is not None:
                nfo_content = self._nfo_content(snap, file, payload, source)
            elif file.file_id in cached_nfo:
                nfo_content = cached_nfo[file.file_id]
                source = "nfo"
            elif nfo_path.is_file() and MediaInfoResolver._nfo_is_usable(nfo_path):
                nfo_content = nfo_path.read_text(encoding="utf-8")
                nfo_content, migrated = normalize_cached_nfo(nfo_content)
                if migrated:
                    LOG.info("NFO_MIGRATE path=%s from=legacy to=v%d", nfo_path, NFO_FORMAT_VERSION)
                source = "nfo"
            else:
                # ffprobe may be unavailable. Keep a valid identity-only NFO rather
                # than dropping the STRM; the quality fallback has already run.
                nfo_content = self._nfo_content(snap, file, None, "unavailable")

            self._write_nfo(nfo_path, nfo_content, snap.hash, file.path, rel_nfo)
            self._write_strm(strm_path, url, snap.hash, file.path, rel_strm)
            desired[rel_strm] = {
                "source_path": file.path,
                "file_id": file.file_id,
                "file_order": file.order,
                "length": file.length,
                "url": url,
                "content_sha256": sha256_text(url + "\n"),
                "nfo": rel_nfo,
                "nfo_sha256": sha256_text(nfo_content),
                "media_info_source": source,
            }

        if snap.category == "tv" and provider_ids_for_snapshot(snap):
            tvshow_path = safe_join(torrent_dir, "tvshow.nfo")
            tvshow_rel = tvshow_path.relative_to(self.root).as_posix()
            self._write_nfo(
                tvshow_path,
                self._tvshow_nfo_content(snap),
                snap.hash,
                snap.title,
                tvshow_rel,
            )
            tvshow_nfo_rel = tvshow_rel
        else:
            tvshow_nfo_rel = None

        old_files = old.get("files", {}) if isinstance(old, dict) else {}
        if isinstance(old_files, dict):
            for rel_path, old_record in old_files.items():
                if rel_path not in desired:
                    stale = safe_join(self.root, *PurePosixPath(rel_path).parts)
                    if stale.is_file() and not self.dry_run:
                        stale.unlink()
                        LOG.info("REMOVE output=%s hash=%s path=%s reason=file_removed_from_torrent", self.spec.name, snap.hash, rel_path)
                    nfo_rel = old_record.get("nfo") if isinstance(old_record, dict) else None
                    if isinstance(nfo_rel, str) and nfo_rel:
                        stale_nfo = safe_join(self.root, *PurePosixPath(nfo_rel).parts)
                        if stale_nfo.is_file() and not self.dry_run:
                            stale_nfo.unlink()
                            LOG.info("REMOVE_NFO output=%s hash=%s path=%s reason=file_removed_from_torrent", self.spec.name, snap.hash, nfo_rel)

        ids = provider_ids(snap.metadata, snap.title)
        metadata_record = {f"{kind}id": value for kind, value in ids.items()}
        return {
            "hash": snap.hash,
            "title": snap.title,
            "category": snap.category,
            "quality": snap.quality,
            "metadata": metadata_record,
            "magnet": snap.magnet or constructed_magnet(snap.hash, snap.title),
            "directory": rel_torrent_dir,
            "tvshow_nfo": tvshow_nfo_rel,
            "mode": self.spec.name,
            "files": desired,
            "directories": sorted(desired_dirs),
        }

    def _remove_stale(self, h: str, record: dict[str, Any], reason: str) -> None:
        if self.spec.name == "kodi":
            LOG.info("KODI_STALE_DEFERRED hash=%s reason=%s", h, reason)
            return
        rel_dir = record.get("directory") if isinstance(record, dict) else None
        if not isinstance(rel_dir, str):
            LOG.warning("STALE_UNKNOWN_PATH output=%s hash=%s reason=%s", self.spec.name, h, reason)
            return
        path = safe_join(self.root, *PurePosixPath(rel_dir).parts)
        self._delete_tree(path, h, reason)

    def _cleanup_unreferenced_kodi_paths(self, old_records: dict[str, Any], new_records: dict[str, Any]) -> None:
        """Delete only previously managed Kodi files that no new record references."""
        if self.spec.name != "kodi":
            return

        def collect(records: dict[str, Any]) -> set[str]:
            paths: set[str] = set()
            for record in records.values():
                if not isinstance(record, dict):
                    continue
                files = record.get("files", {})
                if isinstance(files, dict):
                    for rel, item in files.items():
                        if isinstance(rel, str) and rel:
                            paths.add(rel)
                        if isinstance(item, dict):
                            nfo_rel = item.get("nfo")
                            if isinstance(nfo_rel, str) and nfo_rel:
                                paths.add(nfo_rel)
                tvshow = record.get("tvshow_nfo")
                if isinstance(tvshow, str) and tvshow:
                    paths.add(tvshow)
            return paths

        old_paths = collect(old_records)
        new_paths = collect(new_records)
        for rel in sorted(old_paths - new_paths, key=lambda p: (p.count("/"), p), reverse=True):
            path = safe_join(self.root, *PurePosixPath(rel).parts)
            self._assert_no_symlink(path)
            if path.is_file():
                if self.dry_run:
                    LOG.info("DRY-RUN KODI_REMOVE_MANAGED_FILE path=%s reason=unreferenced_after_sync", rel)
                else:
                    path.unlink()
                    LOG.info("KODI_REMOVE_MANAGED_FILE path=%s reason=unreferenced_after_sync", rel)
            # Prune empty parents but never remove the output root or state dir.
            parent = path.parent
            state_dir = (self.root / self.spec.manifest_rel).parent
            while parent != self.root and parent != state_dir:
                try:
                    if self.dry_run:
                        if any(parent.iterdir()):
                            break
                        LOG.info("DRY-RUN KODI_PRUNE_EMPTY_DIR path=%s", parent.relative_to(self.root).as_posix())
                    else:
                        parent.rmdir()
                        LOG.info("KODI_PRUNE_EMPTY_DIR path=%s", parent.relative_to(self.root).as_posix())
                except OSError:
                    break
                parent = parent.parent

    def run(self, snapshots: dict[str, TorrentSnapshot], old_manifest: dict[str, Any], media_by_hash: dict[str, dict[int, dict[str, Any]]], media_sources_by_hash: dict[str, dict[int, str]], cached_nfo_by_hash: dict[str, dict[int, str]], *, skip_hashes: set[str]) -> tuple[int, int, int, int]:
        self.prepare_root()
        if self.spec.name == "kodi":
            self._prepare_kodi_components(snapshots)
        old_records = old_manifest.get("torrents", {})
        if not isinstance(old_records, dict):
            raise RuntimeError(f"manifest.torrents must be object for output {self.spec.name}")

        for h, record in old_records.items():
            if h not in snapshots:
                self._remove_stale(h, record, "torrent_missing_on_torrserver")

        new_manifest = {
            "version": MANIFEST_VERSION,
            "root": str(self.root),
            "output": self.spec.name,
            "playback": self.spec.playback,
            "generated_at": old_manifest.get("generated_at", int(time.time())),
            "torrserver_url": self.client.base_url,
            "torrents": {},
        }
        created = updated = unchanged = failed = 0
        for h in sorted(skip_hashes):
            old = old_records.get(h)
            if isinstance(old, dict):
                self._remove_stale(h, old, "torrent_removed_by_authoritative_output")
        for h, snap in snapshots.items():
            if h in skip_hashes:
                continue
            old = old_records.get(h)
            try:
                record = self._sync_torrent(snap, old, media_by_hash.get(h, {}), media_sources_by_hash.get(h, {}), cached_nfo_by_hash.get(h, {}))
            except Exception:
                failed += 1
                LOG.exception("ERROR output=%s hash=%s title=%r operation=sync", self.spec.name, h, snap.title)
                if isinstance(old, dict):
                    new_manifest["torrents"][h] = old
                continue
            if record is None:
                continue
            new_manifest["torrents"][h] = record
            if old is None:
                created += 1
            elif old == record:
                unchanged += 1
            else:
                updated += 1

        if self.spec.name == "kodi":
            self._cleanup_unreferenced_kodi_paths(old_records, new_manifest["torrents"])
        if not self.dry_run:
            comparable_old = dict(old_manifest)
            comparable_new = dict(new_manifest)
            comparable_old.pop("generated_at", None)
            comparable_new.pop("generated_at", None)
            if comparable_old != comparable_new:
                new_manifest["generated_at"] = int(time.time())
            else:
                new_manifest["generated_at"] = old_manifest.get("generated_at", int(time.time()))
            if write_manifest_if_changed(self.manifest_path, new_manifest):
                LOG.info("MANIFEST_UPDATED output=%s path=%s", self.spec.name, self.manifest_path)
        return created, updated, unchanged, failed


class SyncCoordinator:
    def __init__(self, cfg: dict[str, Any], client: TorrServerClient, jacred: JacRedClient, *, dry_run: bool):
        self.cfg = cfg
        self.client = client
        self.jacred = jacred
        self.dry_run = dry_run
        self.outputs: list[OutputRunner] = []
        for name in ("jellyfin", "kodi"):
            raw = cfg["outputs"][name]
            spec = OutputSpec(
                name=name,
                root=Path(raw["root"]).expanduser().resolve(),
                manifest_rel=PurePosixPath(raw["manifest"]),
                enabled=bool(raw.get("enabled", True)),
                playback="torrserver" if name == "jellyfin" else "elementum",
                remove_on_missing=bool(raw.get("remove_torrent_on_strm_delete", False)) if name == "jellyfin" else False,
                max_removals=int(raw.get("max_torrent_removals_per_run", 1)) if name == "jellyfin" else 0,
            )
            if spec.enabled:
                self.outputs.append(OutputRunner(spec, cfg, client, dry_run=dry_run))
        if not self.outputs:
            raise ValueError("at least one output must be enabled")
        self.stats = {"jacred_hits": 0, "jacred_misses": 0, "torrserver_ffprobe": 0, "nfo_reused": 0}

    def _get_snapshot_list(self) -> list[TorrentSnapshot]:
        raw_list = self.client.list_torrents()
        snapshots: list[TorrentSnapshot] = []
        for item in raw_list:
            if not isinstance(item, dict):
                raise TorrServerError("TorrServer list contains a non-object item")
            raw_hash = get_field(item, "hash", default=None)
            if not isinstance(raw_hash, str) or not raw_hash.strip():
                raise TorrServerError("TorrServer list item has no hash")
            h = canonical_hash(raw_hash)
            detail = extract_list_embedded_torrent(item)
            if detail is not None:
                merged = dict(item)
                merged.update(detail)
                try:
                    snapshots.append(parse_snapshot(merged, h))
                    continue
                except ValueError as exc:
                    LOG.warning("list embedded metadata unusable hash=%s error=%s; falling back to action=get", h, exc)
            detail = self.client.get_torrent(h)
            list_meta = parse_json_object(get_field(item, "data", default=None))
            detail_meta = parse_json_object(get_field(detail, "data", default=None))
            if list_meta:
                merged_meta = dict(list_meta)
                merged_meta.update({k: v for k, v in detail_meta.items() if v not in (None, "", 0, "null")})
                detail = dict(detail)
                detail["data"] = json.dumps(merged_meta, ensure_ascii=False)
            if not get_field(detail, "title", "name", default=None):
                detail = dict(detail); detail["title"] = get_field(item, "title", "name", default="")
            if not get_field(detail, "category", default=None):
                detail = dict(detail); detail["category"] = get_field(item, "category", default="")
            try:
                snapshots.append(parse_snapshot(detail, h))
                continue
            except ValueError as exc:
                if "FileStats absent/empty" not in str(exc):
                    raise
                LOG.info("METADATA_LOAD hash=%s reason=file_stats_absent", h)
            self.client.load_torrent_metadata(h)
            deadline = time.monotonic() + self.cfg["torrserver"]["metadata_wait_sec"]
            last_detail: dict[str, Any] | None = None
            last_error: Exception | None = None
            while True:
                try:
                    refreshed = self.client.get_torrent(h)
                    last_detail = refreshed
                    snapshots.append(parse_snapshot(refreshed, h))
                    LOG.info("METADATA_READY hash=%s", h)
                    break
                except ValueError as exc:
                    last_error = exc
                    if "FileStats absent/empty" not in str(exc):
                        raise
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"torrent {h}: FileStats still absent after metadata load: {last_error}")
                time.sleep(self.cfg["torrserver"]["metadata_poll_sec"])
        return snapshots

    @staticmethod
    def _merge_jacred_ids(snap: TorrentSnapshot, match: JacRedMatch | None) -> TorrentSnapshot:
        if match is None:
            return snap
        metadata = dict(snap.metadata)
        ids = provider_ids_from_mapping(match.result)
        info = match.result.get("info") if isinstance(match.result.get("info"), dict) else None
        if isinstance(info, dict):
            ids2 = provider_ids(info, str(info.get("title") or ""))
            ids.update({k: v for k, v in ids2.items() if k not in ids})
        for kind, value in ids.items():
            metadata.setdefault(f"{kind}id", value)
        magnet = snap.magnet or match.magnet
        return replace(snap, metadata=metadata, magnet=magnet)

    def _collect_required_files(self, snap: TorrentSnapshot) -> set[int]:
        videos = [f for f in snap.files if is_video(f.path, self.cfg["sync"]["video_extensions"])]
        if not videos:
            return set()
        primary = sorted(videos, key=lambda f: (-f.length, f.path))[0]
        required: set[int] = set()
        for output in self.outputs:
            if output.spec.name == "kodi" and snap.category == "movie":
                required.add(primary.file_id)
            else:
                required.update(f.file_id for f in videos)
        return required

    def run(self) -> int:
        started = time.monotonic()
        enabled_names = ",".join(o.spec.name for o in self.outputs)
        LOG.info("START version=%s torrserver=%s outputs=%s dry_run=%s", VERSION, self.client.base_url, enabled_names, self.dry_run)
        for output in self.outputs:
            output.prepare_root()
        manifests = [(o.root, o.load_manifest()) for o in self.outputs]
        old_by_output = {o.spec.name: manifest for o, (_, manifest) in zip(self.outputs, manifests)}

        raw_snapshots = self._get_snapshot_list()
        snapshot_map: dict[str, TorrentSnapshot] = {s.hash: s for s in raw_snapshots}

        # Exact JacRed match is performed once per torrent, then its ffprobe/category/magnet
        # are shared by both outputs. The query-level cache prevents duplicate HTTP calls.
        matches: dict[str, JacRedMatch | None] = {}
        for h, snap in snapshot_map.items():
            if self.jacred.enabled:
                match = self.jacred.find_match(snap)
                matches[h] = match
                if match is None:
                    self.stats["jacred_misses"] += 1
                else:
                    self.stats["jacred_hits"] += 1
            else:
                matches[h] = None

        enriched_map: dict[str, TorrentSnapshot] = {}
        for h, snap in snapshot_map.items():
            match = matches.get(h)
            updated = self._merge_jacred_ids(snap, match)
            if not updated.category_explicit and match is not None and match.category in {"movie", "tv"}:
                updated = replace(updated, category=match.category)
                LOG.info("CATEGORY hash=%s result=%s via=jacred", updated.hash, updated.category)
            elif not updated.category_explicit and match is not None and match.category == "_uncategorized":
                LOG.info("CATEGORY hash=%s result=_uncategorized via=jacred", updated.hash)
            enriched_map[h] = updated

        media_by_hash: dict[str, dict[int, dict[str, Any]]] = {}
        media_sources_by_hash: dict[str, dict[int, str]] = {}
        cached_nfo_by_hash: dict[str, dict[int, str]] = {}
        quality_ready: dict[str, TorrentSnapshot] = {}
        unresolved = 0
        resolver = MediaInfoResolver(self.cfg, self.client, manifests)
        for h, snap in enriched_map.items():
            old_union = None
            # Quality/state comes from either output manifest; the resolver searches exact source_path NFOs itself.
            required = self._collect_required_files(snap)
            if not required:
                LOG.error("QUALITY_SKIP hash=%s title=%r error=no video files", h, snap.title)
                unresolved += 1
                continue
            try:
                quality, quality_label, probes, sources, cached = resolver.prepare(snap, matches.get(h), required)
            except Exception as exc:
                LOG.error("QUALITY_SKIP hash=%s title=%r error=%s", h, snap.title, exc)
                unresolved += 1
                continue
            media_by_hash[h] = probes
            media_sources_by_hash[h] = sources
            cached_nfo_by_hash[h] = cached
            quality_ready[h] = replace(snap, quality=quality, quality_label=quality_label)

        removed_hashes: set[str] = set()
        totals = {"created": 0, "updated": 0, "unchanged": 0, "failed": unresolved}
        # Jellyfin is authoritative for reverse deletion and is intentionally processed first.
        ordered_outputs = sorted(self.outputs, key=lambda o: 0 if o.spec.name == "jellyfin" else 1)
        for output in ordered_outputs:
            created, updated, unchanged, failed = output.run(
                quality_ready,
                old_by_output[output.spec.name],
                media_by_hash,
                media_sources_by_hash,
                cached_nfo_by_hash,
                skip_hashes=removed_hashes,
            )
            totals["created"] += created
            totals["updated"] += updated
            totals["unchanged"] += unchanged
            totals["failed"] += failed
            if output.spec.name == "jellyfin" and output.removed_hashes:
                removed_hashes.update(output.removed_hashes)

        self.stats["torrserver_ffprobe"] = resolver.stats["torrserver_ffprobe"]
        self.stats["nfo_reused"] = resolver.stats["nfo_reused"]
        elapsed = time.monotonic() - started
        LOG.info(
            "DONE torrents=%d quality_ready=%d quality_unresolved=%d created=%d updated=%d unchanged=%d failed=%d jacred_hits=%d jacred_misses=%d torrserver_ffprobe=%d nfo_reused=%d outputs=%s elapsed=%.2fs",
            len(snapshot_map), len(quality_ready), unresolved, totals["created"], totals["updated"], totals["unchanged"], totals["failed"],
            self.stats["jacred_hits"], self.stats["jacred_misses"], self.stats["torrserver_ffprobe"], self.stats["nfo_reused"], enabled_names, elapsed,
        )
        return 0


def _validate_root_and_manifest(root_value: Any, manifest_value: Any, label: str) -> tuple[str, str]:
    root = str(root_value or "").strip()
    if not root:
        raise ValueError(f"[outputs.{label}].root is required")
    manifest_rel = PurePosixPath(str(manifest_value or ".torr2strm/manifest.json"))
    if manifest_rel.is_absolute() or any(p in {"", ".", ".."} for p in manifest_rel.parts):
        raise ValueError(f"[outputs.{label}].manifest must be a safe relative path")
    return str(Path(root).expanduser()), manifest_rel.as_posix()


def load_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    ts = raw.get("torrserver", {})
    outputs = raw.get("outputs", {})
    sync = raw.get("sync", {})
    quality_cfg = raw.get("quality", {})
    jacred_cfg = raw.get("jacred", {})
    logging_cfg = raw.get("logging", {})
    url = str(ts.get("url", "")).strip().rstrip("/")
    if not url:
        raise ValueError("[torrserver].url is required")
    if not url.startswith(("http://", "https://")):
        raise ValueError("[torrserver].url must start with http:// or https://")

    normalized = []
    exts = sync.get("video_extensions", sorted(VIDEO_EXTENSIONS))
    for ext in exts:
        ext = str(ext).lower().strip()
        if ext and not ext.startswith("."):
            ext = "." + ext
        if ext:
            normalized.append(ext)
    normalized_extensions = set(normalized)

    if not isinstance(outputs, dict):
        raise ValueError("[outputs] must be a table")
    normalized_outputs: dict[str, Any] = {}
    defaults = {
        "jellyfin": {"root": "/mnt/torr2strm-media", "manifest": ".torr2strm/manifest.json", "enabled": True, "remove_torrent_on_strm_delete": False, "max_torrent_removals_per_run": 1},
        "kodi": {"root": "/mnt/torr2strm-media-kodi", "manifest": ".torr2strm/manifest.json", "enabled": True},
    }
    for name in ("jellyfin", "kodi"):
        section = outputs.get(name, {})
        if not isinstance(section, dict):
            raise ValueError(f"[outputs.{name}] must be a table")
        defaults[name].update(section)
        root, manifest = _validate_root_and_manifest(defaults[name]["root"], defaults[name]["manifest"], name)
        max_removals = int(defaults[name].get("max_torrent_removals_per_run", 1))
        if max_removals < 0:
            raise ValueError(f"[outputs.{name}].max_torrent_removals_per_run must be >= 0")
        normalized_outputs[name] = {
            "root": root,
            "manifest": manifest,
            "enabled": bool(defaults[name].get("enabled", True)),
            "remove_torrent_on_strm_delete": bool(defaults[name].get("remove_torrent_on_strm_delete", False)) if name == "jellyfin" else False,
            "max_torrent_removals_per_run": max_removals if name == "jellyfin" else 0,
        }

    jacred_url = str(jacred_cfg.get("url", "")).strip().rstrip("/")
    if jacred_url and not jacred_url.startswith(("http://", "https://")):
        raise ValueError("[jacred].url must start with http:// or https://")
    jacred_indexer_id = int(jacred_cfg.get("indexer_id", 0))
    jacred_limit = int(jacred_cfg.get("limit", 100))
    if jacred_limit < 1 or jacred_limit > 1000:
        raise ValueError("[jacred].limit must be between 1 and 1000")
    jacred_timeout = float(jacred_cfg.get("timeout_sec", 10))
    jacred_retries = int(jacred_cfg.get("retries", 0))
    if jacred_timeout <= 0:
        raise ValueError("[jacred].timeout_sec must be > 0")
    if jacred_retries < 0:
        raise ValueError("[jacred].retries must be >= 0")

    return {
        "torrserver": {
            "url": url,
            "timeout_sec": float(ts.get("timeout_sec", 20)),
            "remove_timeout_sec": float(ts.get("remove_timeout_sec", 60)),
            "metadata_wait_sec": float(ts.get("metadata_wait_sec", 10)),
            "metadata_poll_sec": float(ts.get("metadata_poll_sec", 0.5)),
        },
        "outputs": normalized_outputs,
        "sync": {
            "tv_unmatched_season": int(sync.get("tv_unmatched_season", 0)),
            "video_extensions": normalized_extensions,
        },
        "quality": {
            "timeout_sec": float(quality_cfg.get("timeout_sec", 12)),
            "retries": int(quality_cfg.get("retries", 0)),
        },
        "jacred": {
            "url": jacred_url,
            "api_key": str(jacred_cfg.get("api_key", "")).strip(),
            "indexer_id": jacred_indexer_id,
            "limit": jacred_limit,
            "timeout_sec": jacred_timeout,
            "retries": jacred_retries,
        },
        "logging": {"level": str(logging_cfg.get("level", "INFO")).upper()},
    }


def setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level, logging.INFO), format="%(asctime)s %(levelname)s %(message)s")


def main() -> int:
    parser = argparse.ArgumentParser(description="TorrServer -> multi-output STRM synchronizer")
    parser.add_argument("--config", default="/etc/torr2strm/config.toml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--version", action="version", version=VERSION)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--jacred", metavar="URL", help="override configured JacRed base URL for this run")
    group.add_argument("--no-jacred", action="store_true", help="disable JacRed for this run")
    parser.add_argument("--jacred-api-key", metavar="KEY", help="override configured JacRed API key")
    parser.add_argument("--jacred-indexer-id", type=int, help="override JacRed indexer ID; 0 means all indexers")
    parser.add_argument("--jacred-limit", type=int, help="override JacRed result limit")
    try:
        args = parser.parse_args()
        cfg = load_config(Path(args.config))
        if args.jacred is not None:
            cfg["jacred"]["url"] = args.jacred.rstrip("/")
        if args.no_jacred:
            cfg["jacred"]["url"] = ""
        if args.jacred_api_key is not None:
            cfg["jacred"]["api_key"] = args.jacred_api_key
        if args.jacred_indexer_id is not None:
            cfg["jacred"]["indexer_id"] = args.jacred_indexer_id
        if args.jacred_limit is not None:
            if not 1 <= args.jacred_limit <= 1000:
                raise ValueError("--jacred-limit must be between 1 and 1000")
            cfg["jacred"]["limit"] = args.jacred_limit

        setup_logging(cfg["logging"]["level"])
        client = TorrServerClient(
            cfg["torrserver"]["url"],
            cfg["torrserver"]["timeout_sec"],
            cfg["torrserver"]["remove_timeout_sec"],
        )
        jacred = JacRedClient(cfg["jacred"])
        if jacred.enabled:
            LOG.info("JACRED_ENABLED url=%s indexer_id=%s limit=%s", jacred.base_url, jacred.indexer_id, jacred.limit)
        else:
            LOG.info("JACRED_DISABLED")
        return SyncCoordinator(cfg, client, jacred, dry_run=args.dry_run).run()
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        if not logging.getLogger().handlers:
            logging.basicConfig(level=logging.ERROR, format="%(asctime)s %(levelname)s %(message)s")
        LOG.exception("FATAL %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
