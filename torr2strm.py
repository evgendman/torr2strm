#!/usr/bin/env python3
"""torr2strm 1.4.15 - TorrServer-only STRM/NFO materialization.

Key rules:
- TorrServer is the sole source of torrent identity, category, metadata, and file inventory.
- No external indexer or metadata source is queried.
- Explicitly unsupported categories map to _uncategorized; blank categories use a trusted list fallback or preserve prior managed output when ambiguous.
- Quality root evidence priority: embedded video dimensions, recognized structured release quality/resolution, explicit 4K/2160p markers in the torrent title, then file paths, then 1080p default.
- Jellyfin keeps its existing output layout; Kodi/Elementum keeps its normalized movie/TV tree and playback semantics.
- Cached NFOs may be reused for exact-file streamdetails, but never determine category, quality root, or display quality label.
- Only Jellyfin output may reverse-delete a TorrServer torrent, using action=rem only; never action=drop.
- Recoverable per-torrent source errors do not make the service exit non-zero.
"""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import html
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from typing import Any

VERSION = "1.4.16"
KODI_COMPONENT_MAX_BYTES = 255
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


def sha256_file_text(path: Path, fallback: str) -> str:
    """Hash the bytes actually retained on disk, falling back to planned text."""
    try:
        if path.is_file() and not path.is_symlink():
            return sha256_text(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        pass
    return sha256_text(fallback)


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

def sanitize_kodi_component(value: str) -> str:
    """Make one Kodi path component safe for Windows/SMB clients.

    Jellyfin keeps its historical Linux naming. Kodi replaces characters
    forbidden in Windows names so Samba does not expose generated short aliases.
    """
    clean = sanitize_component(value)
    clean = re.sub(r'[<>:"\\\\|?*]', " - ", clean)
    clean = re.sub(r"\s+", " ", clean).strip().rstrip(". ")
    if re.fullmatch(r"(?i)(?:CON|PRN|AUX|NUL|COM(?:[1-9]|[¹²³])|LPT(?:[1-9]|[¹²³]))(?:\..*)?", clean):
        clean = "_" + clean
    return clean or "_"


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


def bounded_kodi_component(
    value: str,
    identity: str | None = None,
    suffix: str | None = None,
    max_bytes: int = KODI_COMPONENT_MAX_BYTES,
) -> str:
    """Return a Windows-safe Kodi path component within the UTF-8 byte limit.

    Short, unsuffixed components are returned unchanged after normal Kodi
    sanitization. Long components receive a deterministic identity suffix.
    A supplied suffix can preserve a collision convention such as a provider ID
    or short torrent hash when the prefix must be shortened.
    Jellyfin naming deliberately does not use this helper.
    """
    clean = sanitize_kodi_component(value)
    clean_bytes = len(clean.encode("utf-8"))

    if suffix is None and clean_bytes <= max_bytes:
        return clean

    identity_text = str(identity or clean)
    if suffix is not None:
        suffix_clean = sanitize_kodi_component(str(suffix))
        suffix_text = f" [{suffix_clean}]"
        if len(suffix_text.encode("utf-8")) >= max_bytes:
            token = hashlib.sha256(identity_text.encode("utf-8")).hexdigest()[:8]
            suffix_text = f" [{token}]"
        elif clean_bytes + len(suffix_text.encode("utf-8")) <= max_bytes:
            return f"{clean}{suffix_text}"
    else:
        if re.fullmatch(r"(?i)[0-9a-f]{40}", identity_text):
            token = identity_text[:8].lower()
        else:
            token = hashlib.sha256(identity_text.encode("utf-8")).hexdigest()[:8]
        suffix_text = f" [{token}]"

    suffix_bytes = len(suffix_text.encode("utf-8"))
    budget = max_bytes - suffix_bytes
    if budget < 1:
        token = hashlib.sha256(identity_text.encode("utf-8")).hexdigest()[:8]
        suffix_text = f" [{token}]"
        budget = max_bytes - len(suffix_text.encode("utf-8"))
    prefix = truncate_utf8(clean, budget).rstrip(". ")
    if not prefix:
        prefix = "_"
    result = f"{prefix}{suffix_text}"
    if len(result.encode("utf-8")) > max_bytes:
        prefix = truncate_utf8(prefix, max_bytes - len(suffix_text.encode("utf-8"))).rstrip(". ") or "_"
        result = f"{prefix}{suffix_text}"
    return result


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
    """Accept embedded ffprobe JSON and require a real video stream."""
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
    """Parse explicit resolution/interlace/HDR markers; source/codec names alone are insufficient."""
    text = html.unescape(str(value or "")).lower()
    if not text.strip():
        return None

    label: str | None = None
    dimensions = re.search(r"(?<!\d)(\d{3,5})\s*[x×]\s*(\d{3,5})(?!\d)", text)
    if dimensions:
        try:
            label = quality_label_from_dimensions(int(dimensions.group(1)), int(dimensions.group(2)))
        except (TypeError, ValueError):
            label = None

    if label is None:
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

    if label is None:
        return None

    is_dv = bool(re.search(r"\b(?:dolby[ ._-]?vision|dovi|dv)\b", text))
    is_hdr = bool(re.search(r"\b(?:hdr(?:10(?:\+|plus)?)?|hlg)\b", text))
    if is_dv and not label.endswith(" DV"):
        label = re.sub(r"\s+(?:HDR|DV)$", "", label) + " DV"
    elif is_hdr and not label.endswith((" HDR", " DV")):
        label += " HDR"
    return label



def ffprobe_payload_from_metadata(metadata: Any) -> dict[str, Any] | None:
    """Find already-present ffprobe JSON in metadata; never request or calculate it."""
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            return None
    direct = usable_ffprobe(metadata)
    if direct is not None:
        return direct
    if isinstance(metadata, list):
        for item in metadata:
            payload = ffprobe_payload_from_metadata(item)
            if payload is not None:
                return payload
        return None
    if not isinstance(metadata, dict):
        return None
    payload_keys = {"ffprobe", "ffprobejson", "probe", "probejson", "mediainfo", "mediainfojson", "streamdetails", "fileinfo"}
    container_keys = {"info", "metadata", "release", "media", "details", "torrserver", "qualityinfo"}
    for key, value in metadata.items():
        normalized_key = norm_key(str(key))
        if normalized_key in payload_keys:
            payload = ffprobe_payload_from_metadata(value)
            if payload is not None:
                return payload
        elif normalized_key in container_keys and isinstance(value, (dict, list)):
            payload = ffprobe_payload_from_metadata(value)
            if payload is not None:
                return payload
    return None


def quality_labels_from_metadata(metadata: dict[str, Any] | None) -> list[str]:
    """Return every explicit structured quality label, preserving source order."""
    if not isinstance(metadata, dict):
        return []
    accepted = {
        "quality", "qualityname", "qualitylabel", "resolution", "resolutionname",
        "videoquality", "videoresolution", "videotype", "definition",
    }
    nested = {"info", "metadata", "release", "media", "mediainfo", "details", "torrserver", "qualityinfo"}
    result: list[str] = []
    for key, value in metadata.items():
        normalized_key = norm_key(str(key))
        if normalized_key in accepted:
            candidates = value if isinstance(value, (list, tuple)) else [value]
            for candidate in candidates:
                if isinstance(candidate, dict):
                    result.extend(quality_labels_from_metadata(candidate))
                elif candidate not in (None, ""):
                    label = quality_label_from_text(candidate)
                    if label is None and re.fullmatch(r"\s*(?:4320|2160|1440|1080|720|576|480|360|240)\s*", str(candidate)):
                        label = quality_label_from_text(f"{str(candidate).strip()}p")
                    if label:
                        result.append(label)
        elif normalized_key in nested and isinstance(value, dict):
            result.extend(quality_labels_from_metadata(value))
    return list(dict.fromkeys(result))

def quality_label_from_metadata(metadata: dict[str, Any] | None) -> str | None:
    labels = quality_labels_from_metadata(metadata)
    return labels[0] if labels else None


def quality_root_from_label(label: str | None) -> str:
    if not label:
        return "1080p"
    match = re.search(r"(?<!\d)(4320|2160|4k|uhd)(?!\d)", label.lower())
    return "4K" if match else "1080p"


def quality_dimensions_from_probe_payload(payload: Any) -> tuple[int, int] | None:
    """Return the first usable embedded video dimensions without probing media."""
    if isinstance(payload, list):
        streams = payload
    elif isinstance(payload, dict):
        streams = payload.get("streams")
    else:
        return None
    if not isinstance(streams, list):
        return None
    for stream in streams:
        if not isinstance(stream, dict) or str(stream.get("codec_type", "")).lower() != "video":
            continue
        try:
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
        except (TypeError, ValueError):
            continue
        if width > 0 and height > 0:
            return width, height
    return None


def quality_root_from_probe_payload(payload: Any) -> str:
    """Select a quality root from usable dimensions already embedded in TorrServer."""
    dimensions = quality_dimensions_from_probe_payload(payload)
    if dimensions is None:
        return "1080p"
    return quality_from_dimensions(*dimensions)


def has_explicit_4k_marker(value: Any) -> bool:
    """Match standalone 4K/2160p markers without treating codec/source tokens as resolution."""
    text = html.unescape(str(value or "")).lower()
    return bool(re.search(r"(?<![a-z0-9])(?:4k|2160p)(?![a-z0-9])", text))


def quality_root_from_probe_or_title(
    payload: Any,
    title: Any,
    metadata: dict[str, Any] | None = None,
    file_paths: tuple[str, ...] | list[str] = (),
) -> str:
    """Resolve quality from the strongest existing TorrServer evidence, without probing."""
    dimensions = quality_dimensions_from_probe_payload(payload)
    if dimensions is not None:
        return quality_from_dimensions(*dimensions)
    structured_labels = quality_labels_from_metadata(metadata)
    if structured_labels:
        return quality_root_from_label(structured_labels[0])
    if has_explicit_4k_marker(title):
        return "4K"
    if any(has_explicit_4k_marker(path) for path in file_paths):
        return "4K"
    return "1080p"


def quality_marker(label: str | None) -> str:
    return f" — {label}" if label else ""



def bounded_kodi_leaf(title: str, quality_label: str | None, short_hash: str, file_ordinal: int | None = None) -> str:
    """Keep quality/hash suffixes intact even when a human title exceeds NAME_MAX."""
    prefix = sanitize_kodi_component(title)
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

def _episode_coordinates_in_text(value: str) -> tuple[int, int] | None:
    """Parse explicit episode coordinates from one source without mixing evidence."""
    text = html.unescape(str(value or ""))
    patterns = (
        # Accept punctuation separators (especially underscores) after the
        # episode number, but never split adjacent digits or accept a token
        # that continues into letters.
        r"(?i)(?<![A-Z0-9])S(\d{1,2})[ ._-]*E(\d{1,4})(?:[-,](?:E)?\d{1,4})?(?=$|[./\\_\s:+,\-\[\](){}])",
        r"(?i)(?<![A-Z0-9])(\d{1,2})x(\d{1,4})(?:[-,]\d{1,4})?(?=$|[./\\_\s:+,\-\[\](){}])",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def _explicit_season_from_text(value: str) -> int | None:
    """Return an explicit season from one source, without partial digit matches."""
    text = html.unescape(str(value or ""))
    coordinates = _episode_coordinates_in_text(text)
    if coordinates is not None:
        return coordinates[0]
    patterns = (
        r"(?i)(?<![A-Z0-9])Season[ ._-]*(\d{1,2})(?!\d)(?=$|[./\\_\s:+,\-\[\](){}])",
        r"(?i)(?<![A-Z0-9])S(\d{1,2})(?!\d)(?=$|[./\\_\s:+,\-\[\](){}])",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return None


def source_episode_coordinates(path_value: str, torrent_title: str) -> tuple[int | None, int | None]:
    """Resolve coordinates from file path first, using title only as an independent fallback.

    Precedence is intentionally source-aware:
    1. Complete episode coordinates in the file path.
    2. An explicit season in the file path (episode stays unknown).
    3. Complete coordinates in the TorrServer torrent title.
    4. An explicit season in the TorrServer torrent title.

    A known season from the file path is not combined with an episode number from
    the release title. This avoids assigning one title-level episode to every file
    in a multi-file season pack.
    """
    path_coordinates = _episode_coordinates_in_text(path_value)
    if path_coordinates is not None:
        return path_coordinates

    path_season = _explicit_season_from_text(path_value)
    if path_season is not None:
        return path_season, None

    title_coordinates = _episode_coordinates_in_text(torrent_title)
    if title_coordinates is not None:
        return title_coordinates

    return _explicit_season_from_text(torrent_title), None


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
    return sanitize_kodi_component(title or snap.title), year, priority


def clean_media_component(title: str, year: str | None) -> str:
    base = sanitize_kodi_component(title)
    if year and not re.search(rf"\({re.escape(year)}\)$", base):
        base = sanitize_kodi_component(f"{base} ({year})")
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
    coordinates = _episode_coordinates_in_text(stem)
    episode_no = str(coordinates[1]) if coordinates is not None else None
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
    # NFO serialization depends on NFO_FORMAT_VERSION and content, not the
    # application release. A software-only upgrade must not rewrite sidecars.
    header = f'<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n<!-- generated by torr2strm; {source_comment}; nfo_format={NFO_FORMAT_VERSION} -->\n'
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
            headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": f"torr2strm/{VERSION}"},
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
            headers={"Accept": "audio/x-mpegurl,text/plain,*/*", "User-Agent": f"torr2strm/{VERSION}"},
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
    return TorrentSnapshot(torrent_hash, title.strip(), category, metadata, tuple(files), magnet=magnet)


def extract_list_embedded_torrent(item: dict[str, Any]) -> dict[str, Any] | None:
    data = parse_json_object(get_field(item, "data", default=None))
    embedded = data.get("TorrServer") if isinstance(data, dict) else None
    if not isinstance(embedded, dict):
        return None
    raw_files = get_field(embedded, "filestats", "files", default=None)
    return embedded if isinstance(raw_files, list) and raw_files else None


def extract_list_file_stats(item: dict[str, Any]) -> list[Any] | None:
    """Return FileStats already present on the top-level action=list status object."""
    raw_files = get_field(item, "filestats", "files", default=None)
    return raw_files if isinstance(raw_files, list) and raw_files else None


_TRUSTED_TORRSERVER_CATEGORIES = frozenset({"movie", "tv"})


def _normalized_torrserver_category(value: Any) -> str | None:
    if value is None:
        return None
    category = str(value).strip().lower()
    return category or None


def _resolve_snapshot_category(
    detail: dict[str, Any],
    list_item: dict[str, Any],
    torrent_hash: str,
    stage: str,
) -> tuple[dict[str, Any], str | None]:
    """Prefer the current detail response; fall back only when its category is blank."""
    resolved = dict(detail)
    response_category = _normalized_torrserver_category(
        get_field(resolved, "category", default=None)
    )
    if response_category is not None:
        if stage == "metadata_wait":
            status = "trusted" if response_category in _TRUSTED_TORRSERVER_CATEGORIES else "unsupported"
            LOG.info(
                "CATEGORY_SOURCE hash=%s source=action_get_refreshed category=%s status=%s",
                torrent_hash, response_category, status,
            )
        return resolved, response_category

    listed_raw = get_field(list_item, "category", default=None)
    listed_category = _normalized_torrserver_category(listed_raw)
    if listed_category is not None:
        resolved["category"] = listed_raw
        LOG.info(
            "CATEGORY_FALLBACK hash=%s source=action_list category=%s stage=%s",
            torrent_hash, listed_category, stage,
        )
        return resolved, listed_category

    return resolved, None


def _trusted_manifest_categories(manifests: Any) -> dict[str, str]:
    """Return prior trusted categories; mark conflicting output manifests as ambiguous."""
    candidates: dict[str, set[str]] = {}
    for manifest in manifests:
        if not isinstance(manifest, dict):
            continue
        records = manifest.get("torrents", {})
        if not isinstance(records, dict):
            continue
        for raw_hash, record in records.items():
            if not isinstance(record, dict):
                continue
            category = _normalized_torrserver_category(record.get("category"))
            if category not in _TRUSTED_TORRSERVER_CATEGORIES:
                continue
            try:
                torrent_hash = canonical_hash(str(raw_hash))
            except (TypeError, ValueError):
                continue
            candidates.setdefault(torrent_hash, set()).add(category)
    return {
        torrent_hash: next(iter(categories)) if len(categories) == 1 else "conflict"
        for torrent_hash, categories in candidates.items()
    }


def is_video(path_value: str, extensions: set[str]) -> bool:
    return Path(path_value).suffix.lower() in extensions


def season_number(path_value: str, torrent_title: str) -> int | None:
    """Resolve season from the file path before consulting the torrent title."""
    path_season = _explicit_season_from_text(path_value)
    if path_season is not None:
        return path_season
    return _explicit_season_from_text(torrent_title)


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


def bounded_strm_collision_leaf(value: str, identity: str) -> str:
    """Add a stable collision identity and keep the final STRM basename <=255 bytes.

    Ordinary names receive the suffix after the original media filename. If a
    name is too long, truncate its stem at a UTF-8 boundary and retain the
    identity suffix and source extension where possible. The caller adds .strm.
    """
    clean = sanitize_component(value)
    suffix = f" [{identity}]"
    max_leaf_bytes = 255 - len(".strm".encode("utf-8"))
    suffix_bytes = len(suffix.encode("utf-8"))
    if len(clean.encode("utf-8")) + suffix_bytes <= max_leaf_bytes:
        return f"{clean}{suffix}"

    dot = clean.rfind(".")
    ext = clean[dot:] if dot > 0 else ""
    stem = clean[:-len(ext)] if ext else clean
    budget = max_leaf_bytes - suffix_bytes - len(ext.encode("utf-8"))
    if budget < 1:
        # An unusually long source extension must not defeat the filesystem limit.
        ext = ""
        stem = clean
        budget = max_leaf_bytes - suffix_bytes
    if budget < 1:
        raise ValueError("collision identity is too long for a Linux filename")

    prefix = truncate_utf8(stem, budget).rstrip().rstrip(".") or "_"
    result = f"{prefix}{suffix}{ext}"
    if len(result.encode("utf-8")) > max_leaf_bytes:
        budget = max_leaf_bytes - suffix_bytes - len(ext.encode("utf-8"))
        prefix = truncate_utf8(prefix, budget).rstrip().rstrip(".") or "_"
        result = f"{prefix}{suffix}{ext}"
    return result


def plan_strm_leaves(
    targets: list[tuple[TorrentFile, Path, str]],
    torrent_hash: str,
) -> tuple[dict[int, str], dict[int, int]]:
    """Plan unique STRM names before writing any STRM/NFO files.

    Names stay unchanged unless destinations collide. The first collision pass
    adds the short torrent hash and TorrServer file_id to every participant. If
    a destination still collides, the second pass uses the full hash and ID.
    """
    full_hash = canonical_hash(torrent_hash)
    short_hash = full_hash[:8]
    source_by_id: dict[int, tuple[Path, str]] = {}
    for file, target_dir, _ in targets:
        if file.file_id in source_by_id:
            raise ValueError(f"duplicate TorrServer file_id while planning STRM names: {file.file_id}")
        source_by_id[file.file_id] = (target_dir, Path(file.path).name)

    leaves = {
        file_id: bounded_strm_leaf(name, f"{short_hash}-{file_id}")
        for file_id, (_, name) in source_by_id.items()
    }
    collision_levels: dict[int, int] = {}

    def colliding_groups() -> list[list[int]]:
        by_destination: dict[str, list[int]] = {}
        for file_id, (target_dir, _) in source_by_id.items():
            destination = (target_dir / f"{leaves[file_id]}.strm").as_posix()
            by_destination.setdefault(destination, []).append(file_id)
        return [group for group in by_destination.values() if len(group) > 1]

    for group in colliding_groups():
        for file_id in group:
            _, source_name = source_by_id[file_id]
            leaves[file_id] = bounded_strm_collision_leaf(source_name, f"{short_hash}-{file_id}")
            collision_levels[file_id] = 1

    for group in colliding_groups():
        for file_id in group:
            _, source_name = source_by_id[file_id]
            leaves[file_id] = bounded_strm_collision_leaf(source_name, f"{full_hash}-{file_id}")
            collision_levels[file_id] = 2

    unresolved = colliding_groups()
    if unresolved:
        raise RuntimeError(f"unable to resolve STRM destination collisions for TorrServer files: {unresolved!r}")
    return leaves, collision_levels


def _chmod_if_needed(path: Path, mode: int) -> bool:
    """Apply a target mode only when its permission bits actually differ."""
    current_mode = stat.S_IMODE(path.stat().st_mode)
    if current_mode == mode:
        return False
    os.chmod(path, mode)
    return True


def nfo_content_matches(existing: str, planned: str) -> bool:
    """Compare NFO payloads while ignoring legacy app-version-only header changes.

    NFO format markers and all serialized metadata remain significant. Only the
    optional semantic-version token in the generated-by comment is normalized.
    """
    generated_header = re.compile(
        r"(?m)^<!-- generated by torr2strm(?:\s+\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)?;"
    )

    def normalize(value: str) -> str:
        return generated_header.sub("<!-- generated by torr2strm;", value, count=1)

    return normalize(existing) == normalize(planned)


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


SOURCE_CACHE_VERSION = 1


def source_cache_record(snapshot: TorrentSnapshot) -> dict[str, Any]:
    """Serialize the complete, immutable TorrServer file inventory for one infohash."""
    files = [
        {"id": item.file_id, "path": item.path, "length": item.length, "order": item.order}
        for item in sorted(snapshot.files, key=lambda value: value.order)
    ]
    encoded = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"files": files, "files_sha256": sha256_text(encoded)}


def cached_file_stats(record: Any) -> list[dict[str, Any]] | None:
    """Validate cached FileStats before using them to reconstruct a current snapshot."""
    if not isinstance(record, dict):
        return None
    raw_files = record.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        return None

    files: list[dict[str, Any]] = []
    ids: set[int] = set()
    paths: set[str] = set()
    orders: set[int] = set()
    try:
        for row in raw_files:
            if not isinstance(row, dict):
                return None
            file_id = int(row.get("id"))
            path = validate_relative_torrent_path(row.get("path")).as_posix()
            length = int(row.get("length"))
            order = int(row.get("order"))
            if file_id <= 0 or length < 0 or order < 0:
                return None
            if file_id in ids or path in paths or order in orders:
                return None
            ids.add(file_id)
            paths.add(path)
            orders.add(order)
            files.append({"id": file_id, "path": path, "length": length, "order": order})
    except (TypeError, ValueError):
        return None

    if not files or sorted(orders) != list(range(len(files))):
        return None
    files.sort(key=lambda value: value["order"])
    encoded = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if record.get("files_sha256") != sha256_text(encoded):
        return None
    return [
        {"Id": value["id"], "Path": value["path"], "Length": value["length"]}
        for value in files
    ]


def _empty_source_cache(torrserver_url: str) -> dict[str, Any]:
    return {
        "version": SOURCE_CACHE_VERSION,
        "torrserver_url": torrserver_url,
        "torrents": {},
        "projection_signature": None,
    }


def load_source_cache(path: Path, torrserver_url: str) -> dict[str, Any]:
    """Load non-authoritative source-file state; a bad cache degrades to cache misses."""
    if not path.exists():
        return _empty_source_cache(torrserver_url)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOG.warning("SOURCE_CACHE_UNUSABLE path=%s reason=%s; rebuilding from TorrServer", path, exc)
        return _empty_source_cache(torrserver_url)

    if not isinstance(data, dict) or data.get("version") != SOURCE_CACHE_VERSION:
        LOG.warning("SOURCE_CACHE_VERSION_MISMATCH path=%s expected=%d; rebuilding from TorrServer", path, SOURCE_CACHE_VERSION)
        return _empty_source_cache(torrserver_url)
    if data.get("torrserver_url") != torrserver_url:
        LOG.warning("SOURCE_CACHE_SERVER_MISMATCH path=%s cached=%r current=%r; rebuilding from TorrServer", path, data.get("torrserver_url"), torrserver_url)
        return _empty_source_cache(torrserver_url)
    torrents = data.get("torrents")
    if not isinstance(torrents, dict):
        LOG.warning("SOURCE_CACHE_UNUSABLE path=%s reason=invalid_torrents_map; rebuilding from TorrServer", path)
        return _empty_source_cache(torrserver_url)
    projection_signature = data.get("projection_signature")
    if not isinstance(projection_signature, str):
        projection_signature = None
    return {
        "version": SOURCE_CACHE_VERSION,
        "torrserver_url": torrserver_url,
        "torrents": torrents,
        "projection_signature": projection_signature,
    }


def write_source_cache_if_changed(path: Path, data: dict[str, Any]) -> bool:
    encoded = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == encoded:
        return False
    atomic_write_text(path, encoded, mode=0o600)
    return True


def provider_ids_from_mapping(mapping: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(mapping, dict):
        return {}
    result = provider_ids(mapping, str(mapping.get("title") or ""))
    info = mapping.get("info")
    if isinstance(info, dict):
        for kind, value in provider_ids(info, str(info.get("title") or "")).items():
            result.setdefault(kind, value)
    return result


class MediaInfoResolver:
    """Resolve media info once and share it between all output trees."""

    def __init__(self, cfg: dict[str, Any], output_manifests: list[tuple[Path, dict[str, Any]]]):
        self.cfg = cfg
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

    def prepare(
        self,
        torrent: TorrentSnapshot,
        required_file_ids: set[int],
    ) -> tuple[str, str | None, dict[int, dict[str, Any]], dict[int, str], dict[int, str]]:
        """Resolve quality from data already available; never call TorrServer /ffp/."""
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
        for file_id, (_, content) in cached.items():
            cached_contents[file_id] = content
            sources[file_id] = "nfo"
            self.stats["nfo_reused"] += 1

        # Classification is self-contained and TorrServer-only. Cached NFOs
        # preserve exact-file streamdetails but cannot classify the root/label.
        snapshot_payload = ffprobe_payload_from_metadata(torrent.metadata)
        existing_payloads: list[tuple[str, dict[str, Any]]] = []
        if snapshot_payload is not None:
            existing_payloads.append(("torrserver_metadata_ffprobe", snapshot_payload))

        evidence: list[tuple[str, str]] = []
        for source, payload in existing_payloads:
            label = quality_label_from_probe(payload)
            if label:
                evidence.append((source, label))
        for label in quality_labels_from_metadata(torrent.metadata):
            evidence.append(("structured_torrent_quality", label))
        title_label = quality_label_from_text(torrent.title)
        if title_label:
            evidence.append(("release_title", title_label))

        # Root classification is separate from the display label. Priority:
        # embedded dimensions, structured release quality, title marker, then file paths.
        quality_label = evidence[0][1] if evidence else None
        dimensions_available = quality_dimensions_from_probe_payload(snapshot_payload) is not None
        structured_labels = quality_labels_from_metadata(torrent.metadata)
        title_has_4k = has_explicit_4k_marker(torrent.title)
        path_has_4k = any(has_explicit_4k_marker(file.path) for file in torrent.files)
        quality_root = quality_root_from_probe_or_title(
            snapshot_payload, torrent.title, torrent.metadata, [file.path for file in torrent.files]
        )
        root_source = (
            "torrserver_ffprobe_dimensions" if dimensions_available else
            "structured_torrent_quality" if structured_labels else
            "release_title_fallback" if title_has_4k and quality_root == "4K" else
            "release_path_fallback" if path_has_4k and quality_root == "4K" else
            "default_1080p"
        )
        LOG.info(
            "QUALITY_ROOT hash=%s result=%s via=%s",
            torrent.hash, quality_root, root_source,
        )

        # Only ffprobe already present in TorrServer metadata may supply
        # streamdetails to the primary-file NFO. Cached per-file NFOs take precedence.
        if primary.file_id not in cached and existing_payloads:
            source, payload = existing_payloads[0]
            probes[primary.file_id] = payload
            sources[primary.file_id] = source
        for file in required:
            if file.file_id in cached or file.file_id in probes:
                continue
            sources[file.file_id] = "unavailable"

        for source, label in evidence:
            LOG.info("QUALITY_EVIDENCE hash=%s label=%s via=%s", torrent.hash, label, source)
        LOG.info(
            "MEDIAINFO_PLAN hash=%s required_files=%d cached_nfo=%d available_ffprobe_payloads=%d root=%s label=%s torrserver_ffprobe_requests=0",
            torrent.hash, len(required), len(cached), len(existing_payloads), quality_root, quality_label or "unknown",
        )
        return quality_root, quality_label, probes, sources, cached_contents


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
        if self.root.exists() and not self.root.is_dir():
            raise RuntimeError(f"output root is not a directory: {self.root}")
        if self.dry_run:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        _chmod_if_needed(self.root, 0o777)
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        _chmod_if_needed(self.manifest_path.parent, 0o777)

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
                _chmod_if_needed(path, 0o777)
            return
        if self.dry_run:
            LOG.info("DRY-RUN CREATE-DIR output=%s path=%s", self.spec.name, path)
            return
        path.mkdir(parents=True, exist_ok=True)
        _chmod_if_needed(path, 0o777)
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
            name = self.kodi_dirname_by_hash.get(
                snap.hash,
                bounded_kodi_component(snap.title, identity=snap.hash),
            )
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
                    _chmod_if_needed(path, 0o666)
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
            if old is not None and nfo_content_matches(old, content):
                if not self.dry_run:
                    _chmod_if_needed(path, 0o666)
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

    def _prepare_kodi_components(
        self,
        snapshots: dict[str, TorrentSnapshot],
        naming_snapshots: dict[str, TorrentSnapshot] | None = None,
    ) -> None:
        """Build ready output paths using canonical titles/years from all usable inventory items."""
        naming_snapshots = naming_snapshots or snapshots
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

        # Canonical naming is a property of logical identity, not current quality
        # readiness or quality root. Preserve useful title/year metadata from
        # playable inventory items even if technical-quality resolution failed.
        identity_candidates: dict[
            tuple[str, str], list[tuple[int, str, str | None, str]]
        ] = {}
        for h, snap in naming_snapshots.items():
            # Phase 15 deliberately canonicalizes TV series only. Movie directory
            # selection must retain the existing per-quality-root behavior.
            if snap.category != "tv":
                continue
            identity = media_logical_identity(snap)
            if not identity:
                continue
            title, year, priority = explicit_display_title(snap, tv=snap.category == "tv")
            identity_candidates.setdefault((snap.category, identity), []).append(
                (priority, title, year, h)
            )

        canonical_by_identity: dict[tuple[str, str], tuple[str, str | None]] = {}
        for key, candidates in identity_candidates.items():
            candidates.sort(
                key=lambda item: (item[0], item[1].casefold(), item[1], item[3])
            )
            title = candidates[0][1]
            years = {
                item[2]
                for item in candidates
                if item[2] and re.fullmatch(r"(?:19|20)\d{2}", item[2])
            }
            year = min(years, key=lambda value: (int(value), value)) if years else None
            canonical_by_identity[key] = (title, year)

        grouped: dict[tuple[str, str, str], list[tuple[int, str, str | None, str]]] = {}
        fallback: dict[tuple[str, str, str], list[str]] = {}
        for h, snap in snapshots.items():
            quality_root = snap.quality if snap.quality in {"4K", "1080p"} else "1080p"
            identity = media_logical_identity(snap) if snap.category in {"tv", "movie"} else None
            self.kodi_identity_by_hash[h] = identity
            if identity:
                title, year, priority = explicit_display_title(snap, tv=snap.category == "tv")
                if snap.category == "tv":
                    # Series naming can use all parsed inventory snapshots,
                    # including another quality root or a transiently deferred item.
                    title, year = canonical_by_identity.get(
                        ("tv", identity), (title, year)
                    )
                # Movies intentionally retain the previous per-quality-root
                # deterministic selection in base_components below.
                self.kodi_display_title_by_hash[h] = title
                grouped.setdefault((snap.category, quality_root, identity), []).append(
                    (priority, title, year, h)
                )
            else:
                # Unidentified TV/movie torrents and unsupported categories retain
                # their own torrent-title directory and original inner hierarchy.
                name = sanitize_kodi_component(snap.title)
                fallback.setdefault((snap.category, quality_root, name.casefold()), []).append(h)
                self.kodi_display_title_by_hash[h] = name

        # One directory per identity and quality root, using one canonical year/name
        # selected over all available releases of that identity.
        base_components: dict[tuple[str, str, str], tuple[str, str | None]] = {}
        for key, candidates in grouped.items():
            candidates.sort(
                key=lambda item: (item[0], item[1].casefold(), item[1], item[3])
            )
            _, title, year, _ = candidates[0]
            base_components[key] = (title, year)

        raw_components: dict[tuple[str, str, str], str] = {}
        collisions: dict[tuple[str, str, str], set[str]] = {}
        for key, (title, year) in base_components.items():
            category, quality_root, identity = key
            component = clean_media_component(title, year)
            raw_components[key] = component
            collisions.setdefault((category, quality_root, component.casefold()), set()).add(identity)

        for key, (title, year) in base_components.items():
            category, quality_root, identity = key
            component = raw_components[key]
            if len(collisions.get((category, quality_root, component.casefold()), set())) > 1:
                kind, value = identity.split(":", 1)
                component = bounded_kodi_component(
                    component,
                    identity=identity,
                    suffix=f"{kind}-{value}",
                )
            else:
                component = bounded_kodi_component(component, identity=identity)
            for _, _, _, h in grouped[key]:
                self.kodi_dirname_by_hash[h] = component
            # A shared tvshow.nfo uses a deterministic representative among the
            # quality-ready releases in this root, not an unresolved snapshot.
            if category == "tv":
                canonical_hash = grouped[key][0][3]
                self.kodi_series_canonical_ids[key] = provider_ids_for_snapshot(snapshots[canonical_hash])
                self.kodi_series_canonical_hash_by_identity[key] = canonical_hash

        # Add a short hash to an unidentified release directory only if another
        # current torrent would otherwise claim the same directory.
        for key, hashes in fallback.items():
            if len(hashes) == 1:
                h = hashes[0]
                self.kodi_dirname_by_hash[h] = bounded_kodi_component(
                    self.kodi_display_title_by_hash[h],
                    identity=h,
                )
            else:
                for h in hashes:
                    self.kodi_dirname_by_hash[h] = bounded_kodi_component(
                        self.kodi_display_title_by_hash[h],
                        identity=h,
                        suffix=self.kodi_hash_by_torrent[h],
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
        directory_name = self.kodi_dirname_by_hash.get(
            snap.hash,
            bounded_kodi_component(snap.title, identity=snap.hash),
        )
        torrent_dir = safe_join(self.root, snap.category, snap.quality, directory_name)
        self._assert_no_symlink(torrent_dir.parent)
        self._assert_no_symlink(torrent_dir)
        rel_torrent_dir = torrent_dir.relative_to(self.root).as_posix()
        if torrent_dir.exists() and not torrent_dir.is_dir():
            LOG.error("ERROR output=kodi hash=%s destination=%s reason=destination_exists_as_file", snap.hash, torrent_dir)
            return None
        self._mkdir(torrent_dir)

        desired: dict[str, dict[str, Any]] = {}
        nfo_contents_by_file_id: dict[int, str] = {}
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
            if identity:
                base = self.kodi_display_title_by_hash.get(snap.hash) or nfo_movie_title(snap)
            else:
                base, display_year_value, _ = explicit_display_title(snap, tv=False)
                base = clean_media_component(base, display_year_value)
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
                    # Bound each component of the original torrent hierarchy.
                    # Include the torrent hash and source path prefix in the
                    # identity so long sibling names cannot collapse after truncation.
                    source_prefix: list[str] = []
                    safe_parts: list[str] = []
                    for part in PurePosixPath(file.path).parent.parts:
                        source_prefix.append(part)
                        safe_parts.append(
                            bounded_kodi_component(
                                part,
                                identity=f"{snap.hash}:{'/'.join(source_prefix)}",
                            )
                        )
                    target_dir = safe_join(torrent_dir, *safe_parts)
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

            # Keep per-file NFO content in memory so the deterministic representative
            # can supply technical streamdetails to tvshow.nfo, but do not emit an
            # episode-level NFO sidecar in Kodi TV output.
            nfo_contents_by_file_id[file.file_id] = nfo_content
            if snap.category != "tv":
                self._write_nfo(nfo_path, nfo_content, snap.hash, file.path, rel_nfo)
            self._write_strm(strm_path, url, snap.hash, file.path, rel_strm)
            item_record = {
                "source_path": file.path,
                "file_id": file.file_id,
                "file_order": file.order,
                "length": file.length,
                "url": url,
                "content_sha256": sha256_text(url + "\n"),
                "media_info_source": source,
                "quality_root": snap.quality,
                "quality_label": snap.quality_label,
                "logical_identity": identity,
            }
            if snap.category != "tv":
                item_record["nfo"] = rel_nfo
                item_record["nfo_sha256"] = sha256_file_text(nfo_path, nfo_content)
            desired[rel_strm] = item_record

        tvshow_nfo_rel: str | None = None
        tvshow_nfo_sha256: str | None = None
        if normalized_tv:
            group_key = (snap.category, snap.quality or "1080p", identity)
            canonical_hash = self.kodi_series_canonical_hash_by_identity.get(group_key)
            tvshow_path = safe_join(torrent_dir, "tvshow.nfo")
            tvshow_nfo_rel = tvshow_path.relative_to(self.root).as_posix()
            # One stable representative release writes the shared series NFO
            # for the quality root; sibling torrents must not overwrite it.
            if snap.hash == canonical_hash:
                representative_nfo = nfo_contents_by_file_id.get(primary.file_id)
                tvshow_nfo_content = self._tvshow_nfo_content(snap, identity, representative_nfo)
                self._write_nfo(
                    tvshow_path,
                    tvshow_nfo_content,
                    snap.hash,
                    snap.title,
                    tvshow_nfo_rel,
                )
                tvshow_nfo_sha256 = sha256_file_text(tvshow_path, tvshow_nfo_content)

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
            "tvshow_nfo_sha256": tvshow_nfo_sha256,
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
        nfo_contents_by_file_id: dict[int, str] = {}
        desired_dirs: set[str] = {rel_torrent_dir}
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

        planned_leaves, collision_levels = plan_strm_leaves(targets, snap.hash)
        file_by_id = {file.file_id: file for file, _, _ in targets}
        if collision_levels:
            short_hash_files = sum(1 for level in collision_levels.values() if level == 1)
            full_hash_files = sum(1 for level in collision_levels.values() if level > 1)
            files_by_source_name: dict[str, list[TorrentFile]] = {}
            for file_id in sorted(collision_levels):
                file = file_by_id[file_id]
                files_by_source_name.setdefault(Path(file.path).name, []).append(file)
            repeated_name_groups = [
                sorted(group, key=lambda item: (item.path.casefold(), item.file_id))
                for _, group in sorted(files_by_source_name.items(), key=lambda item: item[0].casefold())
                if len(group) > 1
            ]
            if repeated_name_groups:
                example_files = repeated_name_groups[0][:3]
            else:
                example_files = [file_by_id[file_id] for file_id in sorted(collision_levels)[:3]]
            examples = ";".join(
                f"file_id={item.file_id}:{item.path.replace(chr(10), ' ').replace(chr(13), ' ')}"
                for item in example_files
            )
            LOG.warning(
                "STRM_COLLISION_SUMMARY output=%s title=%r hash=%s affected_files=%d short_hash_files=%d full_hash_files=%d examples=%s",
                self.spec.name, snap.title, snap.hash, len(collision_levels),
                short_hash_files, full_hash_files, examples,
            )

        # Keep individual resolution details in the local journal, but send only the title-bearing summary through Apprise.
        for file_id, level in sorted(collision_levels.items()):
            file = file_by_id[file_id]
            if level == 1:
                LOG.warning(
                    "STRM_COLLISION_RESOLVED output=%s hash=%s file_id=%d source=%s suffix=%s",
                    self.spec.name, snap.hash, file_id, file.path, f"{canonical_hash(snap.hash)[:8]}-{file_id}",
                )
            else:
                LOG.warning(
                    "STRM_COLLISION_HASH_EXTENDED output=%s hash=%s file_id=%d source=%s suffix=%s",
                    self.spec.name, snap.hash, file_id, file.path, f"{canonical_hash(snap.hash)}-{file_id}",
                )

        seen_destinations: set[str] = set()
        for file, target_dir, url in targets:
            self._mkdir(target_dir)
            desired_dirs.add(target_dir.relative_to(self.root).as_posix())
            if self.spec.name == "kodi" and snap.category == "movie":
                base_name = bounded_strm_leaf(nfo_movie_title(snap), snap.hash[:8])
                strm_path = safe_join(torrent_dir, base_name + ".strm")
            else:
                base_name = planned_leaves[file.file_id]
                strm_path = safe_join(target_dir, base_name + ".strm")
            rel_strm = strm_path.relative_to(self.root).as_posix()
            nfo_path = strm_path.with_suffix(".nfo")
            rel_nfo = nfo_path.relative_to(self.root).as_posix()
            if rel_strm in seen_destinations:
                LOG.error("STRM_COLLISION_UNRESOLVED output=%s title=%r hash=%s destination=%s source=%s", self.spec.name, snap.title, snap.hash, rel_strm, file.path)
                raise RuntimeError(f"unresolved STRM destination collision: {rel_strm}")
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

            nfo_contents_by_file_id[file.file_id] = nfo_content
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
                "nfo_sha256": sha256_file_text(nfo_path, nfo_content),
                "media_info_source": source,
            }

        tvshow_nfo_sha256: str | None = None
        if snap.category == "tv" and provider_ids_for_snapshot(snap):
            tvshow_path = safe_join(torrent_dir, "tvshow.nfo")
            tvshow_rel = tvshow_path.relative_to(self.root).as_posix()
            tvshow_nfo_content = self._tvshow_nfo_content(
                snap,
                streamdetails_nfo=nfo_contents_by_file_id.get(primary.file_id),
            )
            self._write_nfo(
                tvshow_path,
                tvshow_nfo_content,
                snap.hash,
                snap.title,
                tvshow_rel,
            )
            tvshow_nfo_rel = tvshow_rel
            tvshow_nfo_sha256 = sha256_file_text(tvshow_path, tvshow_nfo_content)
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
            "tvshow_nfo_sha256": tvshow_nfo_sha256,
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

    def run(
        self,
        snapshots: dict[str, TorrentSnapshot],
        old_manifest: dict[str, Any],
        media_by_hash: dict[str, dict[int, dict[str, Any]]],
        media_sources_by_hash: dict[str, dict[int, str]],
        cached_nfo_by_hash: dict[str, dict[int, str]],
        *,
        skip_hashes: set[str],
        deferred_hashes: set[str] | None = None,
        naming_snapshots: dict[str, TorrentSnapshot] | None = None,
    ) -> tuple[int, int, int, int]:
        deferred_hashes = deferred_hashes or set()
        self.prepare_root()
        if self.spec.name == "kodi":
            self._prepare_kodi_components(snapshots, naming_snapshots=naming_snapshots)
        old_records = old_manifest.get("torrents", {})
        if not isinstance(old_records, dict):
            raise RuntimeError(f"manifest.torrents must be object for output {self.spec.name}")

        for h, record in old_records.items():
            if h not in snapshots and h not in deferred_hashes:
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
        for h in deferred_hashes:
            old = old_records.get(h)
            if isinstance(old, dict):
                new_manifest["torrents"][h] = old
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
    def __init__(
        self,
        cfg: dict[str, Any],
        client: TorrServerClient,
        *,
        dry_run: bool,
        notification_capture: NotificationCapture | None = None,
    ):
        self.cfg = cfg
        self.client = client
        self.dry_run = dry_run
        self.notification_capture = notification_capture
        self.source_cache_path = Path(
            cfg["sync"].get("source_cache_path", "/var/lib/torr2strm/source_cache.json")
        ).expanduser()
        self.source_cache_records: dict[str, Any] = {}
        self.source_projection_signature: str | None = None
        self.source_inventory_hashes: set[str] = set()
        self.source_stats: dict[str, int] = {}
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

    def _get_snapshot_list(
        self,
        previous_categories: dict[str, str] | None = None,
    ) -> tuple[list[TorrentSnapshot], set[str]]:
        """Isolate recoverable per-torrent errors and protect prior paths from uncertain categories."""
        previous_categories = previous_categories or {}
        self.source_stats = {
            "action_list": 0, "action_get": 0, "playlist_loads": 0,
            "metadata_poll_gets": 0, "list_embedded": 0, "list_file_stats": 0,
            "source_cache_hits": 0, "source_cache_misses": 0,
        }
        self.source_stats["action_list"] += 1
        raw_list = self.client.list_torrents()
        inventory: list[tuple[dict[str, Any], str]] = []
        for item in raw_list:
            if not isinstance(item, dict):
                raise TorrServerError("TorrServer list contains a non-object item")
            raw_hash = get_field(item, "hash", default=None)
            if not isinstance(raw_hash, str) or not raw_hash.strip():
                raise TorrServerError("TorrServer list item has no hash")
            normalized_hash = canonical_hash(raw_hash)
            inventory.append((item, normalized_hash))
            if self.notification_capture is not None:
                listed_title = get_field(item, "title", "name", default="")
                if isinstance(listed_title, str) and listed_title.strip():
                    self.notification_capture.register_title(normalized_hash, listed_title.strip())

        snapshots: list[TorrentSnapshot] = []
        deferred_hashes: set[str] = set()

        def accept_snapshot(snapshot: TorrentSnapshot, category_value: str | None, stage: str) -> bool:
            if category_value is None:
                previous = previous_categories.get(snapshot.hash)
                if previous is not None:
                    deferred_hashes.add(snapshot.hash)
                    LOG.warning(
                        "CATEGORY_DEFERRED hash=%s title=%r previous=%s stage=%s retry=next_run preserve_existing_output=true",
                        snapshot.hash, snapshot.title, previous, stage,
                    )
                    return False
                LOG.info(
                    "CATEGORY_UNRESOLVED hash=%s title=%r stage=%s disposition=uncategorized",
                    snapshot.hash, snapshot.title, stage,
                )
            # FileStats is immutable for a given BitTorrent infohash. Cache the
            # complete source inventory even when category resolution defers output.
            self.source_cache_records[snapshot.hash] = source_cache_record(snapshot)
            snapshots.append(snapshot)
            if self.notification_capture is not None:
                self.notification_capture.register_title(snapshot.hash, snapshot.title)
            return True

        for item, h in inventory:
            stage = "list_embedded_metadata"
            try:
                detail = extract_list_embedded_torrent(item)
                if detail is not None:
                    merged = dict(item)
                    merged.update(detail)
                    merged, category_value = _resolve_snapshot_category(
                        merged, item, h, stage,
                    )
                    try:
                        snapshot = parse_snapshot(merged, h)
                        self.source_stats["list_embedded"] += 1
                        accept_snapshot(snapshot, category_value, stage)
                        continue
                    except ValueError as exc:
                        LOG.warning(
                            "LIST_EMBEDDED_METADATA_UNUSABLE hash=%s error=%s; trying top-level FileStats/cache before action=get",
                            h, str(exc).replace("\n", " ")[:400],
                        )

                # MatriX.145 already returns FileStats on the top-level list
                # status object for loaded torrents. Prefer this one-call path.
                stage = "list_file_stats"
                list_files = extract_list_file_stats(item)
                if list_files is not None:
                    try:
                        listed = dict(item)
                        listed, category_value = _resolve_snapshot_category(listed, item, h, stage)
                        snapshot = parse_snapshot(listed, h)
                        self.source_stats["list_file_stats"] += 1
                        accept_snapshot(snapshot, category_value, stage)
                        continue
                    except ValueError as exc:
                        LOG.warning(
                            "LIST_FILE_STATS_UNUSABLE hash=%s error=%s; checking source cache",
                            h, str(exc).replace("\n", " ")[:400],
                        )

                # DB-only entries have no FileStats in action=list. Reuse the
                # previously validated full inventory while refreshing mutable
                # fields from the current action=list item.
                stage = "source_cache"
                cached_files = cached_file_stats(self.source_cache_records.get(h))
                if cached_files is not None:
                    try:
                        cached_item = dict(item)
                        cached_item["file_stats"] = cached_files
                        cached_item, category_value = _resolve_snapshot_category(cached_item, item, h, stage)
                        snapshot = parse_snapshot(cached_item, h)
                        self.source_stats["source_cache_hits"] += 1
                        accept_snapshot(snapshot, category_value, stage)
                        continue
                    except ValueError as exc:
                        LOG.warning(
                            "SOURCE_CACHE_ENTRY_UNUSABLE hash=%s error=%s; falling back to action=get",
                            h, str(exc).replace("\n", " ")[:400],
                        )
                self.source_stats["source_cache_misses"] += 1

                stage = "action_get"
                self.source_stats["action_get"] += 1
                detail = self.client.get_torrent(h)
                list_meta = parse_json_object(get_field(item, "data", default=None))
                detail_meta = parse_json_object(get_field(detail, "data", default=None))
                if list_meta:
                    merged_meta = dict(list_meta)
                    merged_meta.update({
                        k: v for k, v in detail_meta.items()
                        if v not in (None, "", 0, "null")
                    })
                    detail = dict(detail)
                    detail["data"] = json.dumps(merged_meta, ensure_ascii=False)
                if not get_field(detail, "title", "name", default=None):
                    detail = dict(detail)
                    detail["title"] = get_field(item, "title", "name", default="")
                detail, category_value = _resolve_snapshot_category(
                    detail, item, h, stage,
                )

                stage = "snapshot_parse"
                try:
                    snapshot = parse_snapshot(detail, h)
                    if accept_snapshot(snapshot, category_value, stage):
                        continue
                    continue
                except ValueError as exc:
                    if "FileStats absent/empty" not in str(exc):
                        raise
                    LOG.info("METADATA_LOAD hash=%s reason=file_stats_absent", h)

                stage = "metadata_load"
                self.source_stats["playlist_loads"] += 1
                self.client.load_torrent_metadata(h)
                deadline = time.monotonic() + self.cfg["torrserver"]["metadata_wait_sec"]
                last_error: Exception | None = None
                while True:
                    stage = "metadata_wait"
                    try:
                        self.source_stats["action_get"] += 1
                        self.source_stats["metadata_poll_gets"] += 1
                        refreshed = self.client.get_torrent(h)
                        refreshed, category_value = _resolve_snapshot_category(
                            refreshed, item, h, stage,
                        )
                        snapshot = parse_snapshot(refreshed, h)
                        accept_snapshot(snapshot, category_value, stage)
                        LOG.info("METADATA_READY hash=%s", h)
                        break
                    except ValueError as exc:
                        last_error = exc
                        if "FileStats absent/empty" not in str(exc):
                            raise
                    except Exception as exc:
                        last_error = exc
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            f"FileStats/metadata still unavailable after metadata load: {last_error}"
                        ) from last_error
                    time.sleep(self.cfg["torrserver"]["metadata_poll_sec"])
            except Exception as exc:
                deferred_hashes.add(h)
                reason = str(exc).replace("\n", " ").strip()[:500] or type(exc).__name__
                reason_code = _source_failure_code(exc, stage, h)
                LOG.warning(
                    "TORRENT_DEFERRED hash=%s stage=%s reason_code=%s error_type=%s reason=%s retry=next_run",
                    h, stage, reason_code, type(exc).__name__, reason,
                )
        # Keep still-listed deferred items; prune only confirmed disappearances.
        self.source_inventory_hashes = {h for _, h in inventory}
        for cached_hash in list(self.source_cache_records):
            if cached_hash not in self.source_inventory_hashes:
                self.source_cache_records.pop(cached_hash, None)
        return snapshots, deferred_hashes

    def _projection_signature(self, snapshots: dict[str, TorrentSnapshot]) -> str:
        """Fingerprint all inputs that can affect either generated output tree."""
        output_config = {
            output.spec.name: {
                "root": str(output.root),
                "manifest": output.spec.manifest_rel.as_posix(),
                "playback": output.spec.playback,
                "remove_on_missing": output.spec.remove_on_missing,
                "max_removals": output.spec.max_removals,
            }
            for output in self.outputs
        }
        inventory = []
        for h, snap in sorted(snapshots.items()):
            inventory.append({
                "hash": h,
                "title": snap.title,
                "category": snap.category,
                "metadata": snap.metadata,
                "magnet": snap.magnet,
                "files": [
                    {
                        "id": item.file_id,
                        "path": item.path,
                        "length": item.length,
                        "order": item.order,
                    }
                    for item in sorted(snap.files, key=lambda value: value.order)
                ],
            })
        payload = {
            "application_version": VERSION,
            "manifest_version": MANIFEST_VERSION,
            "nfo_format_version": NFO_FORMAT_VERSION,
            "sync": {
                "tv_unmatched_season": self.cfg["sync"]["tv_unmatched_season"],
                "video_extensions": sorted(self.cfg["sync"]["video_extensions"]),
            },
            "outputs": output_config,
            "inventory": inventory,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256_text(encoded)

    @staticmethod
    def _expected_output_hashes(snapshots: dict[str, TorrentSnapshot], extensions: set[str]) -> set[str]:
        return {
            h for h, snap in snapshots.items()
            if any(is_video(item.path, extensions) for item in snap.files)
        }

    @staticmethod
    def _regular_managed_file(path: Path) -> bool:
        try:
            return path.is_file() and not path.is_symlink() and (path.stat().st_mode & 0o777) == 0o666
        except OSError:
            return False

    @staticmethod
    def _managed_directory(path: Path) -> bool:
        try:
            return path.is_dir() and not path.is_symlink() and (path.stat().st_mode & 0o777) == 0o777
        except OSError:
            return False

    def _output_manifest_intact(
        self,
        output: OutputRunner,
        manifest: dict[str, Any],
        expected_hashes: set[str],
    ) -> bool:
        """Check managed projections cheaply before skipping a full regeneration pass."""
        records = manifest.get("torrents")
        if not isinstance(records, dict) or set(records) != expected_hashes:
            return False

        shared_nfo_hashes: dict[str, str] = {}
        referenced_shared_nfo: set[str] = set()
        for record in records.values():
            if not isinstance(record, dict):
                return False
            rel = record.get("tvshow_nfo")
            digest = record.get("tvshow_nfo_sha256")
            if rel is None:
                continue
            if not isinstance(rel, str) or not rel:
                return False
            referenced_shared_nfo.add(rel)
            if digest is None:
                # Non-canonical Kodi releases reference the canonical series
                # sidecar but do not claim ownership of its content hash.
                continue
            if not isinstance(digest, str):
                return False
            prior = shared_nfo_hashes.get(rel)
            if prior is not None and prior != digest:
                return False
            shared_nfo_hashes[rel] = digest
        if referenced_shared_nfo != set(shared_nfo_hashes):
            # Older output manifests need one reconciliation to add the hashes.
            return False

        for h, record in records.items():
            if not isinstance(record, dict) or record.get("hash") != h:
                return False
            if record.get("mode") != output.spec.name:
                return False
            directory = record.get("directory")
            if not isinstance(directory, str) or not directory:
                return False
            try:
                directory_path = safe_join(output.root, *PurePosixPath(directory).parts)
                output._assert_no_symlink(directory_path)
            except Exception:
                return False
            if not self._managed_directory(directory_path):
                return False

            directories = record.get("directories", [])
            if not isinstance(directories, list):
                return False
            for rel_dir in directories:
                if not isinstance(rel_dir, str) or not rel_dir:
                    return False
                try:
                    path = safe_join(output.root, *PurePosixPath(rel_dir).parts)
                    output._assert_no_symlink(path)
                except Exception:
                    return False
                if not self._managed_directory(path):
                    return False

            files = record.get("files")
            if not isinstance(files, dict) or not files:
                return False
            for rel_strm, file_record in files.items():
                if not isinstance(rel_strm, str) or not rel_strm or not isinstance(file_record, dict):
                    return False
                try:
                    strm_path = safe_join(output.root, *PurePosixPath(rel_strm).parts)
                    output._assert_no_symlink(strm_path)
                except Exception:
                    return False
                if not self._regular_managed_file(strm_path):
                    return False
                expected_url = file_record.get("url")
                if not isinstance(expected_url, str):
                    return False
                try:
                    if strm_path.read_text(encoding="utf-8") != expected_url:
                        return False
                except (OSError, UnicodeDecodeError):
                    return False

                nfo_rel = file_record.get("nfo")
                if nfo_rel is not None:
                    if not isinstance(nfo_rel, str) or not nfo_rel:
                        return False
                    try:
                        nfo_path = safe_join(output.root, *PurePosixPath(nfo_rel).parts)
                        output._assert_no_symlink(nfo_path)
                    except Exception:
                        return False
                    expected_nfo_hash = file_record.get("nfo_sha256")
                    if not self._regular_managed_file(nfo_path) or not isinstance(expected_nfo_hash, str):
                        return False
                    try:
                        if sha256_text(nfo_path.read_text(encoding="utf-8")) != expected_nfo_hash:
                            return False
                    except (OSError, UnicodeDecodeError):
                        return False

            tvshow_rel = record.get("tvshow_nfo")
            if tvshow_rel is not None:
                if not isinstance(tvshow_rel, str) or not tvshow_rel:
                    return False
                try:
                    tvshow_path = safe_join(output.root, *PurePosixPath(tvshow_rel).parts)
                    output._assert_no_symlink(tvshow_path)
                except Exception:
                    return False
                if not self._regular_managed_file(tvshow_path):
                    return False
                expected_hash = shared_nfo_hashes.get(tvshow_rel)
                if not isinstance(expected_hash, str):
                    return False
                try:
                    if sha256_text(tvshow_path.read_text(encoding="utf-8")) != expected_hash:
                        return False
                except (OSError, UnicodeDecodeError):
                    return False
        return True

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

        previous_categories = _trusted_manifest_categories(old_by_output.values())
        cache = load_source_cache(self.source_cache_path, self.client.base_url)
        self.source_cache_records = cache["torrents"]
        self.source_projection_signature = cache.get("projection_signature")
        raw_snapshots, deferred_hashes = self._get_snapshot_list(previous_categories)
        snapshot_map: dict[str, TorrentSnapshot] = {s.hash: s for s in raw_snapshots}
        if not deferred_hashes and len(snapshot_map) == len(self.source_inventory_hashes):
            current_signature = self._projection_signature(snapshot_map)
            expected_hashes = self._expected_output_hashes(snapshot_map, self.cfg["sync"]["video_extensions"])
            projections_intact = all(
                self._output_manifest_intact(output, old_by_output[output.spec.name], expected_hashes)
                for output in self.outputs
            )
            if self.source_projection_signature == current_signature and projections_intact:
                unchanged = sum(len(old_by_output[output.spec.name].get("torrents", {})) for output in self.outputs)
                LOG.info(
                    "FAST_PATH inventory=%d outputs=%s reason=signature_match_and_managed_files_intact",
                    len(snapshot_map), enabled_names,
                )
                elapsed = time.monotonic() - started
                LOG.info(
                    "DONE torrents=%d quality_ready=%d quality_unresolved=0 created=0 updated=0 unchanged=%d failed=0 nfo_reused=0 outputs=%s source_requests=list:%d,get:%d,playlist:%d,polls:%d cache_hits=%d cache_misses=%d list_embedded=%d list_file_stats=%d fast_path=true elapsed=%.2fs",
                    len(snapshot_map), len(expected_hashes), unchanged, enabled_names,
                    self.source_stats.get("action_list", 0), self.source_stats.get("action_get", 0),
                    self.source_stats.get("playlist_loads", 0), self.source_stats.get("metadata_poll_gets", 0),
                    self.source_stats.get("source_cache_hits", 0), self.source_stats.get("source_cache_misses", 0),
                    self.source_stats.get("list_embedded", 0), self.source_stats.get("list_file_stats", 0), elapsed,
                )
                return 0
            if self.source_projection_signature == current_signature:
                LOG.info("FAST_PATH_DECLINED reason=managed_output_drift_or_manifest_mismatch; running_full_reconciliation")

        # Category, metadata, IDs and magnets remain those already present on
        # TorrServer. No external enrichment pass is performed.
        media_by_hash: dict[str, dict[int, dict[str, Any]]] = {}
        media_sources_by_hash: dict[str, dict[int, str]] = {}
        cached_nfo_by_hash: dict[str, dict[int, str]] = {}
        quality_ready: dict[str, TorrentSnapshot] = {}
        quality_deferred_hashes: set[str] = set()
        permanent_skip_hashes: set[str] = set()
        unresolved = 0
        resolver = MediaInfoResolver(self.cfg, manifests)
        for h, snap in snapshot_map.items():
            # Quality/state comes from either output manifest; the resolver searches exact source_path NFOs itself.
            required = self._collect_required_files(snap)
            if not required:
                # No playable video is a permanent, deterministic skip. Do not
                # retain old managed output forever as if the source were transiently unreadable.
                permanent_skip_hashes.add(h)
                LOG.error(
                    "QUALITY_SKIP hash=%s title=%r reason=no_playable_video_files disposition=cleanup_managed_outputs",
                    h, snap.title,
                )
                unresolved += 1
                continue
            try:
                quality, quality_label, probes, sources, cached = resolver.prepare(snap, required)
            except Exception as exc:
                # Inventory/identity is readable, but this run cannot reliably
                # classify/materialize it. Preserve old outputs and retry next run.
                quality_deferred_hashes.add(h)
                LOG.error(
                    "QUALITY_DEFERRED hash=%s title=%r error=%s retry=next_run preserve_existing_output=true",
                    h, snap.title, str(exc).replace("\\n", " ")[:500],
                )
                unresolved += 1
                continue
            media_by_hash[h] = probes
            media_sources_by_hash[h] = sources
            cached_nfo_by_hash[h] = cached
            quality_ready[h] = replace(snap, quality=quality, quality_label=quality_label)

        # Canonical Kodi names may use all successfully parsed snapshots with
        # playable video, including an item with temporary quality-resolution failure.
        # Output and tvshow.nfo representatives still come only from quality_ready.
        naming_snapshots = {
            h: quality_ready.get(h, snap)
            for h, snap in snapshot_map.items()
            if h not in permanent_skip_hashes
        }
        retained_hashes = set(deferred_hashes) | quality_deferred_hashes

        removed_hashes: set[str] = set()
        output_failures = 0
        totals = {"created": 0, "updated": 0, "unchanged": 0, "failed": unresolved + len(deferred_hashes)}
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
                deferred_hashes=retained_hashes,
                naming_snapshots=naming_snapshots,
            )
            totals["created"] += created
            totals["updated"] += updated
            totals["unchanged"] += unchanged
            totals["failed"] += failed
            output_failures += failed
            if output.spec.name == "jellyfin" and output.removed_hashes:
                removed_hashes.update(output.removed_hashes)

        if not self.dry_run:
            # Persist a whole-inventory projection signature only when every
            # listed torrent has a definitive outcome and every output succeeded.
            # A later run must still validate managed files before taking FAST_PATH.
            projection_signature = None
            if not deferred_hashes and not quality_deferred_hashes and output_failures == 0:
                projection_signature = self._projection_signature(snapshot_map)
            cache_to_write = {
                "version": SOURCE_CACHE_VERSION,
                "torrserver_url": self.client.base_url,
                "torrents": self.source_cache_records,
                "projection_signature": projection_signature,
            }
            try:
                if write_source_cache_if_changed(self.source_cache_path, cache_to_write):
                    LOG.info(
                        "SOURCE_CACHE_UPDATED path=%s torrents=%d projection_signature=%s",
                        self.source_cache_path, len(self.source_cache_records),
                        "set" if projection_signature else "cleared",
                    )
            except OSError:
                LOG.exception("SOURCE_CACHE_WRITE_FAILED path=%s", self.source_cache_path)

        elapsed = time.monotonic() - started
        LOG.info(
            "DONE torrents=%d quality_ready=%d quality_unresolved=%d created=%d updated=%d unchanged=%d failed=%d nfo_reused=%d outputs=%s source_requests=list:%d,get:%d,playlist:%d,polls:%d cache_hits=%d cache_misses=%d list_embedded=%d list_file_stats=%d fast_path=false elapsed=%.2fs",
            len(snapshot_map) + len(deferred_hashes), len(quality_ready), unresolved + len(deferred_hashes), totals["created"], totals["updated"], totals["unchanged"], totals["failed"],
            resolver.stats["nfo_reused"], enabled_names,
            self.source_stats.get("action_list", 0), self.source_stats.get("action_get", 0),
            self.source_stats.get("playlist_loads", 0), self.source_stats.get("metadata_poll_gets", 0),
            self.source_stats.get("source_cache_hits", 0), self.source_stats.get("source_cache_misses", 0),
            self.source_stats.get("list_embedded", 0), self.source_stats.get("list_file_stats", 0), elapsed,
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

    notifications = raw.get("notifications", {})
    if not isinstance(notifications, dict):
        raise ValueError("[notifications] must be a table")
    notify_enabled = bool(notifications.get("enabled", False))
    apprise_url = str(notifications.get("apprise_url", "")).strip().rstrip("/")
    apprise_config_key = str(notifications.get("apprise_config_key", "arr-stack")).strip()
    service_name = str(notifications.get("service_name", "torr2strm")).strip() or "torr2strm"
    notify_timeout = int(notifications.get("timeout_sec", 5))
    tag_warning = str(notifications.get("tag_warning", "warnings")).strip()
    tag_critical = str(notifications.get("tag_critical", "critical")).strip()
    state_path = str(notifications.get("state_path", "/var/lib/torr2strm/notification_state.json")).strip()
    source_cache_path = str(sync.get("source_cache_path", "/var/lib/torr2strm/source_cache.json")).strip()
    if not source_cache_path:
        raise ValueError("[sync].source_cache_path must not be empty")
    if notify_enabled:
        if not apprise_url.startswith(("http://", "https://")):
            raise ValueError("[notifications].apprise_url must start with http:// or https:// when enabled")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", apprise_config_key):
            raise ValueError("[notifications].apprise_config_key must contain only letters, digits, '_' or '-'")
        if not tag_warning or not tag_critical:
            raise ValueError("[notifications].tag_warning and tag_critical must not be empty")
        if not 1 <= notify_timeout <= 30:
            raise ValueError("[notifications].timeout_sec must be between 1 and 30")
        if not state_path:
            raise ValueError("[notifications].state_path must not be empty")
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
            "source_cache_path": str(Path(source_cache_path).expanduser()),
        },
        "notifications": {
            "enabled": notify_enabled,
            "apprise_url": apprise_url,
            "apprise_config_key": apprise_config_key,
            "service_name": service_name,
            "timeout_sec": notify_timeout,
            "tag_warning": tag_warning,
            "tag_critical": tag_critical,
            "state_path": str(Path(state_path).expanduser()),
        },
        "logging": {"level": str(logging_cfg.get("level", "INFO")).upper()},
    }


def setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level, logging.INFO), format="%(asctime)s %(levelname)s %(message)s")


class NotificationCapture(logging.Handler):
    """Capture diagnostic records and context for user-facing Apprise summaries."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.torrent_titles: dict[str, str] = {}
        self.exception_details: dict[str, str] = {}

    def register_title(self, torrent_hash: str, title: str) -> None:
        normalized_hash = canonical_hash(str(torrent_hash))
        clean_title = str(title).replace("\r", " ").replace("\n", " ").strip()
        if normalized_hash and clean_title:
            self.torrent_titles[normalized_hash] = clean_title[:300]

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage().replace("\r", " ").replace("\n", " ").strip()
            # These are successful housekeeping actions, intermediate fallback
            # diagnostics, delivery diagnostics, or deliberately detailed per-file
            # collision entries. They remain in the local journal.
            if message.startswith((
                "APPRISE_",
                "STRM_COLLISION_RESOLVED ",
                "STRM_COLLISION_HASH_EXTENDED ",
                "LIST_EMBEDDED_METADATA_UNUSABLE ",
                "REMOVE-TREE ",
                "INTERRUPTED ",
            )):
                return
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                detail = f"{type(exc).__name__}: {exc}".replace("\r", " ").replace("\n", " ").strip()
                self.exception_details[message] = detail[:700]
            if record.levelno >= logging.ERROR:
                self.errors.append(message)
            elif record.levelno >= logging.WARNING:
                self.warnings.append(message)
        except Exception:
            self.handleError(record)


def _notification_events(values: list[str]) -> list[str]:
    return sorted({str(v).replace("\r", " ").replace("\n", " ").strip()[:500] for v in values if str(v).strip()})


def _source_failure_code(exc: Exception, stage: str, torrent_hash: str) -> str:
    """Classify recoverable TorrServer errors without leaking the hash as the cause."""
    if isinstance(exc, TorrServerNotFound):
        return "not_found"
    message = str(exc).lower()
    if "timed out" in message or "timeout" in message:
        return "timeout"
    if "cannot connect" in message or "connection refused" in message or "network is unreachable" in message:
        return "connection"
    if "invalid json" in message or "not an object" in message or "not an array" in message:
        return "invalid_response"
    if "http 404" in message or (stage == "action_get" and message.strip() == torrent_hash.lower()):
        return "not_found"
    if "http " in message:
        return "http_error"
    if "filestats" in message or "metadata" in message or "playlist" in message:
        return "metadata_unavailable"
    return "unknown"


def _log_field(raw: str, name: str) -> str:
    match = re.search(rf"(?:^|\s){re.escape(name)}=([^\s]+)", raw)
    return match.group(1) if match else ""


def _log_value_before(raw: str, name: str, next_name: str) -> str:
    match = re.search(
        rf"(?:^|\s){re.escape(name)}=(.*?)\s+{re.escape(next_name)}=",
        raw,
    )
    return match.group(1).strip() if match else ""


def _log_title(raw: str) -> str:
    patterns = (
        r"\btitle=(.+?)\s+hash=[0-9a-fA-F]{20,}\b",
        r"\btitle=(.+?)\s+reason=",
        r"\btitle=(.+?)\s+error=",
        r"\btitle=(.+?)\s+retry=",
        r"\btitle=(.+?)\s+operation=",
    )
    for pattern in patterns:
        match = re.search(pattern, raw)
        if not match:
            continue
        candidate = match.group(1).strip()
        try:
            value = ast.literal_eval(candidate)
        except (ValueError, SyntaxError):
            value = candidate.strip(chr(39) + chr(34))
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _safe_display_text(value: str, limit: int = 240) -> str:
    return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())[:limit]


def _source_failure_text(reason_code: str, stage: str, raw_reason: str) -> str:
    if reason_code == "not_found":
        return "TorrServer не нашёл раздачу при запросе подробностей (HTTP 404)."
    if reason_code == "connection":
        return "Не удалось подключиться к TorrServer."
    if reason_code == "timeout":
        return "Истекло время ожидания ответа TorrServer."
    if reason_code == "invalid_response":
        return "TorrServer вернул ответ в неожиданном или некорректном формате."
    if reason_code == "http_error":
        status = re.search(r"HTTP\s+(\d{3})", raw_reason, re.IGNORECASE)
        suffix = f" (HTTP {status.group(1)})." if status else "."
        return "TorrServer отклонил запрос" + suffix
    if reason_code == "metadata_unavailable" or stage in {"metadata_load", "metadata_wait", "snapshot_parse"}:
        return "TorrServer пока не предоставил необходимые сведения о файлах раздачи."
    return "Не удалось получить подробности раздачи из TorrServer."


def _parse_notification_event(raw: str, category: str, capture: NotificationCapture) -> dict[str, Any]:
    torrent_hash = _log_field(raw, "hash").lower()
    title = _log_title(raw)
    if not title and torrent_hash:
        title = capture.torrent_titles.get(torrent_hash, "")
    output = _log_field(raw, "output")
    output_label = {"jellyfin": "Jellyfin", "kodi": "Kodi/Elementum"}.get(output, "")
    suffix = f" (выход: {output_label})" if output_label else ""
    event: dict[str, Any] = {
        "heading": "",
        "title": title,
        "hash": torrent_hash,
        "output": output,
        "text": "",
        "details": [],
        "raw": raw,
        "category": category,
    }

    if raw.startswith("TORRENT_DEFERRED "):
        stage = _log_field(raw, "stage")
        reason_code = _log_field(raw, "reason_code")
        reason = _log_value_before(raw, "reason", "retry")
        if not reason:
            reason_match = re.search(r"\breason=(.*)$", raw)
            reason = reason_match.group(1).strip() if reason_match else ""
        if not reason_code:
            reason_code = _source_failure_code(
                TorrServerNotFound(torrent_hash) if stage == "action_get" and reason == torrent_hash else RuntimeError(reason),
                stage,
                torrent_hash,
            )
        event["heading"] = "Временно не удалось получить данные о раздачах"
        event["text"] = (
            _source_failure_text(reason_code, stage, reason)
            + " Обработка отложена до следующего запуска; существующие управляемые файлы сохраняются."
        )
        if stage:
            stage_labels = {
                "list_embedded_metadata": "разбор сведений из списка",
                "action_get": "запрос подробностей",
                "snapshot_parse": "разбор сведений о файлах",
                "metadata_load": "загрузка метаданных",
                "metadata_wait": "ожидание метаданных",
            }
            event["details"].append(f"Этап: {stage_labels.get(stage, stage)}.")
        if reason_code == "unknown" and reason and reason != torrent_hash:
            event["details"].append("Техническая причина: " + _safe_display_text(reason, 180))
    elif raw.startswith("QUALITY_DEFERRED "):
        event["heading"] = "Временно не удалось определить качество медиа"
        event["text"] = (
            "Не удалось надёжно определить качество или подготовить сведения о видеофайлах. "
            "Существующие STRM/NFO и запись манифеста сохранены; повторная попытка будет в следующем запуске."
        )
        error = _log_value_before(raw, "error", "retry")
        if error:
            event["details"].append("Причина: " + _safe_display_text(error, 200))
    elif raw.startswith("QUALITY_SKIP "):
        event["heading"] = "Медиа пропущено: нет поддерживаемых видеофайлов"
        event["text"] = (
            "В раздаче не найдено видеофайлов поддерживаемых форматов. "
            "Эта раздача будет пропущена, а ранее созданные управляемые STRM/NFO будут очищены, если они есть."
        )
    elif raw.startswith("STRM_COLLISION_SUMMARY "):
        event["heading"] = "Совпадения имён STRM разрешены автоматически"
        affected = int(_log_field(raw, "affected_files") or "0")
        short_count = int(_log_field(raw, "short_hash_files") or "0")
        full_count = int(_log_field(raw, "full_hash_files") or "0")
        event["text"] = (
            f"У {affected} файлов совпадали целевые имена. Сервис автоматически сделал имена STRM уникальными "
            f"(короткий идентификатор: {short_count}; полный хеш: {full_count})."
        )
        examples = _log_value_before(raw, "examples", "__no_such_field__")
        if not examples:
            match = re.search(r"\bexamples=(.*)$", raw)
            examples = match.group(1) if match else ""
        paths = []
        for part in examples.split(";"):
            match = re.match(r"file_id=(\d+):(.*)", part)
            if match:
                paths.append(f"{match.group(2)} (ID файла {match.group(1)})")
        if paths:
            event["details"].append("Примеры исходных файлов: " + "; ".join(paths[:3]) + ".")
    elif raw.startswith("STRM_COLLISION_UNRESOLVED "):
        event["heading"] = "Конфликт имён STRM не удалось разрешить"
        event["text"] = (
            "Безопасно создать все выходные файлы не удалось. Обработка этой раздачи прервана, "
            "чтобы не перезаписать один файл другим."
        )
        source = _log_value_before(raw, "source", "__no_such_field__")
        if not source:
            match = re.search(r"\bsource=(.*)$", raw)
            source = match.group(1) if match else ""
        destination = _log_value_before(raw, "destination", "source")
        if destination:
            event["details"].append("Конфликтующий целевой путь: " + _safe_display_text(destination, 220) + ".")
        if source:
            event["details"].append("Исходный файл: " + _safe_display_text(source, 220) + ".")
    elif raw.startswith("STRM_MISSING "):
        event["heading"] = "Обнаружены отсутствующие STRM-файлы"
        count = _log_field(raw, "count") or "неизвестное число"
        event["text"] = f"В медиатеке отсутствуют {count} STRM-файлов, которые числятся в манифесте сервиса."
        match = re.search(r"\bfirst=(.*)$", raw)
        if match:
            event["details"].append("Первый обнаруженный путь: " + _safe_display_text(match.group(1), 220) + ".")
    elif raw.startswith("SEASON_FALLBACK "):
        event["heading"] = "Не удалось определить сезон сериала"
        season = _log_field(raw, "season") or "00"
        file_path = _log_value_before(raw, "file", "season")
        event["text"] = (
            f"Сезон не удалось определить по имени файла; сервис поместит его в Season {season}."
        )
        if file_path:
            event["details"].append("Файл: " + _safe_display_text(file_path, 220) + ".")
    elif raw.startswith("PATH_CONFLICT "):
        event["heading"] = "Конфликт каталогов медиатеки"
        if "reason=unmanaged_directory_exists" in raw:
            destination = _log_value_before(raw, "destination", "reason")
            event["text"] = (
                "Целевой каталог уже существует, но не зарегистрирован как управляемый каталог сервиса. "
                "Синхронизация этой раздачи остановлена, каталог не перезаписывался."
            )
            if destination:
                event["details"].append("Существующий каталог: " + _safe_display_text(destination, 220) + ".")
        else:
            old_path = _log_value_before(raw, "old", "new")
            new_path = _log_value_before(raw, "new", "__no_such_field__")
            if not new_path:
                match = re.search(r"\bnew=(.*)$", raw)
                new_path = match.group(1) if match else ""
            event["text"] = (
                "Старый и новый каталоги существуют одновременно. Сервис не стал переносить медиа "
                "автоматически, чтобы не затронуть существующие данные."
            )
            if old_path:
                event["details"].append("Старый каталог: " + _safe_display_text(old_path, 180) + ".")
            if new_path:
                event["details"].append("Новый каталог: " + _safe_display_text(new_path, 180) + ".")
    elif raw.startswith("ABORT-REMOVE "):
        event["heading"] = "Раздача не удалена из-за защитного лимита"
        limit = _log_field(raw, "limit") or "неизвестно"
        event["text"] = (
            f"Сервис не удалил раздачу: достигнут лимит обратного удаления за один запуск ({limit}). "
            "Это ограничение защищает от массового удаления при ошибках в медиатеке."
        )
    elif raw.startswith("REMOVE ") and "reason=strm_deleted_by_user" in raw:
        event["heading"] = "Обнаружены отсутствующие STRM-файлы"
        event["text"] = (
            "В соответствии с настройкой обратного удаления связанная раздача удалена из TorrServer "
            "после удаления управляемого STRM-файла."
        )
    elif raw.startswith("ERROR ") and "operation=rem" in raw:
        event["heading"] = "Не удалось удалить раздачу из TorrServer"
        event["text"] = (
            "После обнаружения удалённого STRM-файла сервис попытался удалить связанную раздачу, "
            "но TorrServer не подтвердил успешное удаление."
        )
        detail = capture.exception_details.get(raw)
        if detail:
            event["details"].append("Причина: " + _safe_display_text(detail, 220))
    elif raw.startswith("ERROR_NFO "):
        event["heading"] = "Не удалось записать файл NFO"
        destination = _log_value_before(raw, "destination", "reason")
        event["text"] = "На месте ожидаемого файла NFO находится каталог. Сервис не смог обновить метаданные."
        if destination:
            event["details"].append("Путь: " + _safe_display_text(destination, 220) + ".")
    elif raw.startswith("ERROR ") and "reason=destination_is_directory" in raw:
        event["heading"] = "Не удалось записать STRM-файл"
        destination = _log_value_before(raw, "destination", "reason")
        event["text"] = "На месте ожидаемого STRM-файла находится каталог. Файл не был перезаписан."
        if destination:
            event["details"].append("Путь: " + _safe_display_text(destination, 220) + ".")
    elif raw.startswith("ERROR ") and "reason=destination_exists_as_file" in raw:
        event["heading"] = "Не удалось создать каталог медиа"
        destination = _log_value_before(raw, "destination", "reason")
        event["text"] = "На месте целевого каталога находится обычный файл. Сервис не изменял его и не продолжил эту раздачу."
        if destination:
            event["details"].append("Путь: " + _safe_display_text(destination, 220) + ".")
    elif raw.startswith("STALE_UNKNOWN_PATH "):
        event["heading"] = "Не удалось безопасно очистить устаревшую запись"
        event["text"] = (
            "У записи манифеста не найден путь к управляемому каталогу. "
            "Сервис не стал удалять файлы по предположительному пути."
        )
    elif raw.startswith("NFO_FORMAT_NEWER "):
        version = _log_field(raw, "version") or "неизвестная"
        current = _log_field(raw, "current") or "неизвестная"
        event["heading"] = "Обнаружен файл NFO более нового формата"
        event["text"] = (
            f"Файл NFO использует формат {version}, а текущая версия сервиса поддерживает формат {current}. "
            "Исходное содержимое сохранено без изменений."
        )
    elif raw.startswith("TVSHOW_STREAMDETAILS_SKIP "):
        event["heading"] = "Не удалось добавить технические сведения в NFO сериала"
        event["text"] = "Сведения о видеопотоке не удалось перенести в tvshow.nfo; остальная синхронизация продолжается."
        error = _log_value_before(raw, "error", "__no_such_field__")
        if not error:
            match = re.search(r"\berror=(.*)$", raw)
            error = match.group(1) if match else ""
        if error:
            event["details"].append("Причина: " + _safe_display_text(error, 180))
    elif raw.startswith("ERROR ") and "operation=sync" in raw:
        event["heading"] = "Не удалось синхронизировать медиа"
        event["text"] = "Обработка этой раздачи завершилась ошибкой; существующая запись манифеста сохранена."
        detail = capture.exception_details.get(raw)
        if detail:
            event["details"].append("Ошибка: " + _safe_display_text(detail, 220))
    elif raw.startswith("FATAL "):
        event["heading"] = "Сервис завершил запуск с критической ошибкой"
        event["title"] = ""
        event["text"] = "Запуск torr2strm аварийно завершился. Проверьте журнал сервиса для полного traceback."
        detail = raw[len("FATAL "):].strip()
        if detail:
            event["details"].append("Ошибка: " + _safe_display_text(detail, 220))
    elif raw.startswith("REMOVE "):
        event["heading"] = "Изменение медиатеки"
        event["text"] = "Сервис выполнил удаление раздачи из TorrServer."
    elif raw.startswith("STRM_MISSING "):
        event["heading"] = "Обнаружены отсутствующие STRM-файлы"
        event["text"] = "Некоторые STRM-файлы из манифеста не найдены."
    else:
        # Unknown events are still visible, but do not expose an opaque raw log line
        # as the user-facing sentence.
        event["heading"] = "Другие предупреждения" if category == "warnings" else "Другие ошибки"
        event["text"] = "Обнаружено событие, требующее внимания. Подробности доступны в журнале torr2strm."
        code_match = re.match(r"([A-Z][A-Z0-9_-]{2,})", raw)
        if code_match:
            event["details"].append("Тип события в журнале: " + code_match.group(1) + ".")
        detail = capture.exception_details.get(raw)
        if detail:
            event["details"].append("Ошибка: " + _safe_display_text(detail, 220))

    if suffix and not raw.startswith("STRM_COLLISION_SUMMARY "):
        event["text"] += suffix
    if not event["title"] and torrent_hash:
        event["title"] = f"Раздача {torrent_hash[:8]}"
    return event


def _render_notification_body(values: list[str], category: str, capture: NotificationCapture) -> str:
    raw_events = _notification_events(values)
    parsed = [_parse_notification_event(raw, category, capture) for raw in raw_events]
    if not parsed:
        return ""

    # The sync-level traceback may repeat a more specific per-torrent diagnostic
    # (for example, STRM_COLLISION_UNRESOLVED followed by the raised RuntimeError).
    # Keep the actionable event and avoid showing the same failure twice.
    explicit_failure_hashes = {
        event["hash"] for event in parsed
        if event["hash"] and event["heading"] in {
            "Конфликт имён STRM не удалось разрешить",
            "Не удалось записать STRM-файл",
            "Не удалось записать файл NFO",
            "Не удалось создать каталог медиа",
        }
    }
    parsed = [
        event for event in parsed
        if not (
            event["heading"] == "Не удалось синхронизировать медиа"
            and event["hash"] in explicit_failure_hashes
        )
    ]

    # A repeated readable media title can refer to different torrent releases.
    title_hashes: dict[str, set[str]] = {}
    for event in parsed:
        if event["title"]:
            title_hashes.setdefault(event["title"].casefold(), set()).add(event["hash"])
    for event in parsed:
        if event["title"] and event["hash"] and len(title_hashes.get(event["title"].casefold(), set())) > 1:
            event["title"] += f" (раздача {event['hash'][:8]})"

    grouped: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for event in parsed:
        heading = event["heading"]
        identity = (event["hash"], event["title"] or "")
        section = grouped.setdefault(heading, {})
        if identity not in section:
            section[identity] = {
                "title": event["title"],
                "hash": event["hash"],
                "texts": [],
                "details": [],
            }
        item = section[identity]
        if event["text"] and event["text"] not in item["texts"]:
            item["texts"].append(event["text"])
        for detail in event["details"]:
            if detail and detail not in item["details"]:
                item["details"].append(detail)

    heading_order = [
        "Сервис завершил запуск с критической ошибкой",
        "Не удалось синхронизировать медиа",
        "Не удалось безопасно очистить устаревшую запись",
        "Конфликт имён STRM не удалось разрешить",
        "Конфликт каталогов медиатеки",
        "Не удалось создать каталог медиа",
        "Не удалось записать STRM-файл",
        "Не удалось записать файл NFO",
        "Не удалось удалить раздачу из TorrServer",
        "Раздача не удалена из-за защитного лимита",
        "Медиа пропущено: нет поддерживаемых видеофайлов",
        "Временно не удалось определить качество медиа",
        "Временно не удалось получить данные о раздачах",
        "Обнаружены отсутствующие STRM-файлы",
        "Не удалось определить сезон сериала",
        "Совпадения имён STRM разрешены автоматически",
        "Раздача удалена по настройке обратного удаления",
        "Изменение медиатеки",
        "Обнаружен файл NFO более нового формата",
        "Не удалось добавить технические сведения в NFO сериала",
        "Другие предупреждения",
        "Другие ошибки",
    ]
    rank = {heading: index for index, heading in enumerate(heading_order)}
    sections = sorted(grouped.items(), key=lambda pair: (rank.get(pair[0], len(rank)), pair[0]))
    opening = (
        "Во время запуска torr2strm обнаружены предупреждения."
        if category == "warnings"
        else "Во время запуска torr2strm обнаружены ошибки."
    )
    lines = [opening]
    omitted = 0
    budget = 3150

    for heading, items in sections:
        block = ["", heading]
        ordered_items = sorted(items.values(), key=lambda item: (str(item["title"]).casefold(), item["hash"]))
        for item in ordered_items:
            title = item["title"] or "Медиа без определённого названия"
            description = " ".join(item["texts"])
            media_line = f"• «{title}» — {description}" if item["title"] else f"• {description}"
            detail_lines = [f"  - {detail}" for detail in item["details"]]
            candidate = block + [media_line] + detail_lines
            if len("\n".join(lines + candidate)) <= budget:
                block = candidate
            else:
                # Retain media identity and a concise issue summary even when
                # long paths/details cannot fit in the Telegram-sized message.
                compact = f"• «{title}» — {description[:180]}"
                if len("\n".join(lines + block + [compact])) <= budget:
                    block.append(compact)
                else:
                    omitted += 1
        if len(block) > 2:
            if len("\n".join(lines + block)) <= budget:
                lines.extend(block)
            else:
                omitted += len(items)

    if omitted:
        lines.append("")
        lines.append(f"Ещё {omitted} событий не вошли в сообщение; полные подробности сохранены в журнале.")
    lines.append("")
    lines.append("Технические подробности доступны в журнале сервиса.")
    body = "\n".join(lines)
    if len(body) > 3450:
        body = body[:3350].rstrip() + "\n\nПодробности и список событий сохранены в журнале."
    return body[:3500]


def apprise_notify(config: dict[str, Any], *, title: str, body: str, notify_type: str, tag: str) -> bool:
    endpoint = config["apprise_url"].rstrip("/") + "/notify/" + urllib.parse.quote(config["apprise_config_key"], safe="")
    payload = json.dumps({"title": title, "body": body[:3500], "type": notify_type, "tag": tag}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=payload, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": f"torr2strm/{VERSION}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=config["timeout_sec"]) as response:
            status = int(getattr(response, "status", 200))
        LOG.info("APPRISE_SENT tag=%s status=%d", tag, status)
        return 200 <= status < 300
    except urllib.error.HTTPError as exc:
        LOG.warning("APPRISE_FAILED tag=%s http_status=%d", tag, exc.code)
    except urllib.error.URLError as exc:
        LOG.warning("APPRISE_FAILED tag=%s error_type=%s", tag, type(exc.reason).__name__)
    except Exception as exc:
        LOG.warning("APPRISE_FAILED tag=%s error_type=%s", tag, type(exc).__name__)
    return False


def dispatch_run_notifications(cfg: dict[str, Any], capture: NotificationCapture) -> None:
    config = cfg.get("notifications", {})
    if not isinstance(config, dict) or not config.get("enabled"):
        return

    state_path = Path(config["state_path"])
    state: dict[str, Any] = {}
    try:
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            state = loaded
    except FileNotFoundError:
        pass
    except Exception as exc:
        LOG.warning("APPRISE_STATE_READ_FAILED error_type=%s", type(exc).__name__)

    service_name = str(config["service_name"])
    for category, kind, tag in (
        ("warnings", "warning", config["tag_warning"]),
        ("errors", "failure", config["tag_critical"]),
    ):
        values = capture.warnings if category == "warnings" else capture.errors
        body = _render_notification_body(values, category, capture)
        # Fingerprint the actual user-facing message, so a newly resolved media
        # title or actionable cause can update a previously identical log event.
        fingerprint = sha256_text(body) if body else ""
        if body and fingerprint != str(state.get(category, "")):
            apprise_notify(
                config,
                title=f"{service_name}: {'warnings' if category == 'warnings' else 'critical'}",
                body=f"Service: {service_name}\n\n{body}",
                notify_type=kind,
                tag=tag,
            )
        state[category] = fingerprint
    try:
        atomic_write_text(state_path, json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n", mode=0o600)
    except Exception as exc:
        LOG.warning("APPRISE_STATE_WRITE_FAILED error_type=%s", type(exc).__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="TorrServer -> multi-output STRM synchronizer")
    parser.add_argument("--config", default="/etc/torr2strm/config.toml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--version", action="version", version=VERSION)
    cfg: dict[str, Any] | None = None
    capture: NotificationCapture | None = None
    try:
        args = parser.parse_args()
        cfg = load_config(Path(args.config))
        setup_logging(cfg["logging"]["level"])
        capture = NotificationCapture()
        LOG.addHandler(capture)
        client = TorrServerClient(cfg["torrserver"]["url"], cfg["torrserver"]["timeout_sec"], cfg["torrserver"]["remove_timeout_sec"])
        return SyncCoordinator(
            cfg, client, dry_run=args.dry_run, notification_capture=capture
        ).run()
    except KeyboardInterrupt:
        LOG.warning("INTERRUPTED synchronization cancelled")
        return 130
    except Exception as exc:
        if not logging.getLogger().handlers:
            logging.basicConfig(level=logging.ERROR, format="%(asctime)s %(levelname)s %(message)s")
        LOG.exception("FATAL %s", exc)
        return 1
    finally:
        if cfg is not None and capture is not None:
            try:
                dispatch_run_notifications(cfg, capture)
            except Exception as exc:
                LOG.warning("APPRISE_DISPATCH_FAILED error_type=%s", type(exc).__name__)
            LOG.removeHandler(capture)


if __name__ == "__main__":
    raise SystemExit(main())