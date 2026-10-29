import json
import logging
import os
import stat
import tempfile
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torr2strm as torr2strm_module
from torr2strm import (
    MediaInfoResolver,
    NotificationCapture,
    dispatch_run_notifications,
    OutputRunner,
    OutputSpec,
    SyncCoordinator,
    bounded_strm_leaf,
    bounded_strm_collision_leaf,
    bounded_kodi_leaf,
    bounded_kodi_component,
    sanitize_kodi_component,
    extract_btih,
    media_quality,
    quality_label_from_probe,
    quality_label_from_text,
    quality_label_from_metadata,
    quality_root_from_label,
    quality_root_from_probe_payload,
    quality_root_from_probe_or_title,
    parse_snapshot,
    season_number,
    source_episode_coordinates,
    usable_ffprobe,
    atomic_write_text,
    migrate_nfo_v1_to_v2,
    nfo_format_version,
)


class FakeClient:
    base_url = "http://127.0.0.1:8092"

    def __init__(self, torrents, probes=None):
        self.torrents = {t["Hash"]: t for t in torrents}
        self.removed = []
        self.probes = probes or {}
        self.ffprobe_calls = []

    def list_torrents(self):
        return [
            {"Hash": h, "Title": t["Title"], "Category": t.get("Category", ""), "Data": t.get("Data", "")}
            for h, t in self.torrents.items()
        ]

    def get_torrent(self, h):
        from torr2strm import TorrServerNotFound
        if h not in self.torrents:
            raise TorrServerNotFound(h)
        return self.torrents[h]

    def remove_torrent(self, h):
        self.removed.append(h)
        self.torrents.pop(h, None)

    def torrent_present(self, h):
        return h in self.torrents

    def get_ffprobe(self, h, file_id, timeout=None):
        self.ffprobe_calls.append((h, file_id))
        key = (h, file_id)
        payload = self.probes.get(key)
        if payload is None:
            raise RuntimeError(f"no fake probe for {key}")
        return payload

    def load_torrent_metadata(self, h):
        return None


class SequenceResponseClient(FakeClient):
    """Use a stable action=list row and explicit ordered action=get responses."""

    def __init__(self, list_item, responses):
        super().__init__([list_item])
        self.list_item = list_item
        self.responses = list(responses)
        self.response_index = 0

    def list_torrents(self):
        item = self.list_item
        return [{
            "Hash": item["Hash"],
            "Title": item["Title"],
            "Category": item.get("Category", ""),
            "Data": item.get("Data", ""),
        }]

    def get_torrent(self, h):
        if h != self.list_item["Hash"]:
            raise KeyError(h)
        index = min(self.response_index, len(self.responses) - 1)
        self.response_index += 1
        return self.responses[index]


def cfg(jelly_root, kodi_root, remove=False):
    return {
        "torrserver": {
            "url": "http://127.0.0.1:8092",
            "timeout_sec": 2,
            "remove_timeout_sec": 5,
            "metadata_wait_sec": 0,
            "metadata_poll_sec": 0.01,
        },
        "outputs": {
            "jellyfin": {
                "root": str(jelly_root),
                "manifest": ".torr2strm/manifest.json",
                "enabled": True,
                "remove_torrent_on_strm_delete": remove,
                "max_torrent_removals_per_run": 1,
            },
            "kodi": {
                "root": str(kodi_root),
                "manifest": ".torr2strm/manifest.json",
                "enabled": True,
            },
        },
        "sync": {
            "tv_unmatched_season": 0,
            "video_extensions": {".mkv", ".mp4"},
        },
        "quality": {"timeout_sec": 2, "retries": 0},
        "notifications": {
            "enabled": False,
            "apprise_url": "",
            "apprise_config_key": "arr-stack",
            "service_name": "torr2strm test",
            "timeout_sec": 5,
            "tag_warning": "warnings",
            "tag_critical": "critical",
            "state_path": str(Path(jelly_root).parent / "notification_state.json"),
        },
        "logging": {"level": "ERROR"},
    }


def torrent(h="a" * 40, title="Film", category="movie", paths=None, lengths=None, data=None):
    paths = paths or ["Film.mkv"]
    lengths = lengths or [100] * len(paths)
    files = [
        {"Id": i + 1, "Path": p, "Length": lengths[i] if i < len(lengths) else 100}
        for i, p in enumerate(paths)
    ]
    return {
        "Hash": h,
        "Title": title,
        "Category": category,
        "Data": json.dumps(data or {}, ensure_ascii=False),
        "FileStats": files,
    }




def attach_existing_probe(t, payload):
    data = json.loads(t.get("Data") or "{}")
    data["ffprobe"] = payload
    t["Data"] = json.dumps(data, ensure_ascii=False)


def nfo_root_element(text):
    """Parse the XML root, excluding a trailing Kodi Combination NFO URL."""
    import re
    import xml.etree.ElementTree as ET
    end = re.search(r"</(?:movie|episodedetails|tvshow)>", text)
    assert end is not None
    return ET.fromstring(text[:end.end()])


def assert_no_display_title_fields(text):
    root = nfo_root_element(text)
    for tag in ("title", "originaltitle", "sorttitle", "showtitle", "name", "year", "premiered", "releasedate", "aired"):
        assert root.find(tag) is None, f"unexpected display/localization field {tag!r} in NFO root"

def probe(width=1920, height=1080, codec="h264", audio=True, hdr=None):
    video = {
        "index": 0,
        "codec_name": codec,
        "codec_type": "video",
        "width": width,
        "height": height,
        "bit_rate": "5000000",
        "avg_frame_rate": "24000/1001",
        "tags": {"BPS": "5000000", "DURATION": "00:10:00.000000000"},
    }
    if hdr == "dv":
        video["dv_profile"] = 8
        video["color_transfer"] = "smpte2084"
    elif hdr == "hdr10":
        video["color_transfer"] = "smpte2084"
    streams = [video]
    if audio:
        streams.append({
            "index": 1,
            "codec_name": "ac3",
            "codec_type": "audio",
            "sample_rate": "48000",
            "channels": 2,
            "channel_layout": "stereo",
            "bit_rate": "384000",
            "tags": {"title": "RUS", "language": "rus", "DURATION": "00:10:00.000000000"},
        })
    return {"streams": streams}


def run(cfg_data, client):
    return SyncCoordinator(cfg_data, client, dry_run=False).run()


def test_extract_btih_accepts_magnet():
    h = "064de6b3010e8a7f8df1098b8bab407af0405f86"
    assert extract_btih(f"magnet:?xt=urn:btih:{h}&dn=test") == h


def test_episode_parser_accepts_underscore_delimiters_and_prefers_file_path_evidence():
    cases = [
        ("Black.Mirror.S03E01_Dub_RHS.mkv", "Black Mirror S04", (3, 1)),
        ("Squid.Game.S02E01_RHS_1080p.mkv", "Squid Game S03", (2, 1)),
        ("Lucifer.S02E01.1080p.WEB-DL.5xRus.Eng.mkv", "Lucifer S01", (2, 1)),
        ("Show.1x02_Dub.mkv", "Show S04E03", (1, 2)),
        ("Disc/Season_12/episode01.mkv", "Series S04E02", (12, None)),
        ("episode01.mkv", "Black Mirror S04", (4, None)),
        ("episode01.mkv", "Show.S03E01.1080p", (3, 1)),
        ("episode01.mkv", "Show.Season_03.1080p", (3, None)),
    ]
    for path, title, expected in cases:
        assert source_episode_coordinates(path, title) == expected, (path, title)
        assert season_number(path, title) == expected[0], (path, title)


def test_episode_parser_rejects_partial_season_digits_and_ambiguous_tokens():
    malformed = (
        "Show.S123E01.mkv",
        "Show.S03E01episode.mkv",
        "Show.Season123.mkv",
        "Show.Episode1201.mkv",
    )
    for path in malformed:
        assert source_episode_coordinates(path, "") == (None, None), path
        assert season_number(path, "") is None, path

    # A valid two-digit season is never shortened to season zero/one.
    assert source_episode_coordinates("Show.S03E01_Dub.mkv", "") == (3, 1)
    assert source_episode_coordinates("Show.S02E01_RHS.mkv", "") == (2, 1)
    assert season_number("Show.S03E01_Dub.mkv", "") == 3
    assert season_number("Show.S02E01_RHS.mkv", "") == 2


def test_phase19_seasons_and_episode_coordinates_reach_jellyfin_and_kodi_outputs():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        black_mirror = {
            "seriesTmdbId": 335984,
            "seriesTitle": "Black Mirror",
            "seriesYear": 2011,
        }
        squid_game = {
            "seriesTmdbId": 93405,
            "seriesTitle": "Squid Game",
            "seriesYear": 2021,
        }
        items = [
            torrent(
                h="a" * 40, title="Black Mirror S03 1080p WEB-DL", category="tv",
                paths=["Black.Mirror.S03E01_Dub_RHS.mkv", "Black.Mirror.S03E02_Dub_RHS.mkv"],
                data=black_mirror,
            ),
            torrent(
                h="b" * 40, title="Black Mirror S04 1080p WEB-DL", category="tv",
                paths=["Black.Mirror.S04E01_Dub_RHS.mkv"],
                data=black_mirror,
            ),
            torrent(
                h="c" * 40, title="Squid Game Season 02 1080p WEB-DL", category="tv",
                paths=["Squid.Game.S02E01_RHS_1080p.mkv"],
                data=squid_game,
            ),
            torrent(
                h="d" * 40, title="Squid Game Season 03 1080p WEB-DL", category="tv",
                paths=["Squid.Game.S03E01_RHS_1080p.mkv"],
                data=squid_game,
            ),
        ]
        client = FakeClient(items)
        assert run(cfg(jr, kr), client) == 0

        jelly_strms = list((jr / "tv" / "1080p").rglob("*.strm"))
        kodi_strms = list((kr / "tv" / "1080p").rglob("*.strm"))
        assert len(jelly_strms) == 5
        assert len(kodi_strms) == 5
        assert not any("Season 00" in p.parts for p in jelly_strms + kodi_strms)

        expected = [
            ("a" * 40, "Black.Mirror.S03E01_Dub_RHS.mkv", 3, 1),
            ("a" * 40, "Black.Mirror.S03E02_Dub_RHS.mkv", 3, 2),
            ("b" * 40, "Black.Mirror.S04E01_Dub_RHS.mkv", 4, 1),
            ("c" * 40, "Squid.Game.S02E01_RHS_1080p.mkv", 2, 1),
            ("d" * 40, "Squid.Game.S03E01_RHS_1080p.mkv", 3, 1),
        ]
        for torrent_hash, source_name, season, episode in expected:
            jelly_strm = next(p for p in jelly_strms if p.name == source_name + ".strm")
            assert f"Season {season:02d}" in jelly_strm.parts
            jelly_nfo = jelly_strm.with_suffix(".nfo")
            assert jelly_nfo.is_file()
            nfo_text = jelly_nfo.read_text(encoding="utf-8")
            assert f"<season>{season}</season>" in nfo_text
            assert f"<episode>{episode}</episode>" in nfo_text

            kodi_strm = next(
                p for p in kodi_strms
                if f"S{season:02d}E{episode:02d}" in p.name
                and torrent_hash[:8] in p.name
            )
            assert f"Season {season:02d}" in kodi_strm.parts


def test_quality_root_and_display_label_are_separate_and_quality_fallback_is_explicit():
    assert media_quality(probe(1920, 1080)) == "1080p"
    assert media_quality(probe(3840, 1600)) == "4K"
    assert media_quality(probe(2560, 1440)) == "1080p"
    assert quality_label_from_probe(probe(2560, 1440)) == "1440p"
    assert quality_label_from_probe(probe(1280, 720)) == "720p"
    assert quality_label_from_probe(probe(3840, 2160, hdr="dv")) == "2160p DV"
    assert quality_label_from_text("Release.WEB-DL.1080p.HDR") == "1080p HDR"
    assert quality_label_from_text("Release 1920x1080 WEB-DL") == "1080p"
    assert quality_label_from_text("Release 3840x2160 HDR") == "2160p HDR"
    assert quality_label_from_text("Release 4K Dolby Vision") == "2160p DV"
    assert quality_label_from_text("WEB-DL HEVC") is None
    assert quality_label_from_metadata({"quality": "720p WEB-DL"}) == "720p"
    assert quality_root_from_label("720p") == "1080p"
    assert quality_root_from_label("1440p") == "1080p"
    assert quality_root_from_label("2160p HDR") == "4K"
    assert usable_ffprobe(probe()) is not None


def test_long_utf8_strm_nfo_names_are_bounded_and_atomic_write_succeeds():
    long_name = (
        "Престиж - The Prestige - 2006 - ДБ, ПМ, ПД, АП (Гаврилов, Визгунов, "
        "Сербин, Королев), СТ - 4K, HEVC, Dolby Vision Profile 8 - WEB-DL "
        "(2160p) | Дубляж | Сербин | Гаврилов | Визгунов | Королев"
    )
    leaf = bounded_strm_leaf(long_name, "deec5dec")
    assert len((leaf + ".strm").encode("utf-8")) <= 255
    assert len((leaf + ".nfo").encode("utf-8")) <= 255
    assert leaf.endswith("[deec5dec]")
    with tempfile.TemporaryDirectory() as td:
        nfo = Path(td) / (leaf + ".nfo")
        atomic_write_text(nfo, "<movie/>\n")
        assert nfo.read_text(encoding="utf-8") == "<movie/>\n"



def test_bounded_strm_collision_leaf_uses_identity_and_respects_name_max():
    short = bounded_strm_collision_leaf("VTS_01_1.VOB", "aa7edb49-12")
    assert short == "VTS_01_1.VOB [aa7edb49-12]"
    assert len((short + ".strm").encode("utf-8")) <= 255
    assert len((short + ".nfo").encode("utf-8")) <= 255

    full_hash = "aa7edb49969f25ebbdfb2c4bb0b2c0ed68f11c55"
    long_source = ("ОченьДлинноеИмяDVD" * 35) + ".VOB"
    extended = bounded_strm_collision_leaf(long_source, f"{full_hash}-123456789")
    assert len((extended + ".strm").encode("utf-8")) <= 255
    assert len((extended + ".nfo").encode("utf-8")) <= 255
    assert extended.endswith(f" [{full_hash}-123456789].VOB")


def test_jellyfin_multidisc_repeated_vob_names_are_all_materialized_without_changing_normal_names(caplog):
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h = "a" * 40
        paths = [
            "DVD2/VIDEO_TS/VTS_01_0.VOB",
            "DVD3/VIDEO_TS/VTS_01_0.VOB",
            "DVD4/VIDEO_TS/VTS_01_0.VOB",
            "DVD2/VIDEO_TS/VTS_01_1.VOB",
            "DVD3/VIDEO_TS/VTS_01_1.VOB",
            "DVD2/VIDEO_TS/Unique.mkv",
        ]
        t = torrent(
            h=h,
            title="Lexx (1997) Season 01 DVD collection",
            category="tv",
            paths=paths,
            lengths=[600, 590, 580, 570, 560, 100],
            data={},
        )
        p = probe(1920, 1080)
        attach_existing_probe(t, p)
        client = FakeClient([t], probes={(h, file_id): p for file_id in range(1, len(paths) + 1)})
        config = cfg(jr, kr)
        config["sync"]["video_extensions"] = {".vob", ".mkv"}
        config["outputs"]["kodi"]["enabled"] = False
        caplog.set_level(logging.WARNING, logger="torr2strm")

        assert run(config, client) == 0

        summaries = [
            record.getMessage() for record in caplog.records
            if record.getMessage().startswith("STRM_COLLISION_SUMMARY ")
        ]
        assert len(summaries) == 1
        assert "title='Lexx (1997) Season 01 DVD collection'" in summaries[0]
        assert "hash=" + h in summaries[0]
        assert "affected_files=5" in summaries[0]
        assert "short_hash_files=5" in summaries[0]
        assert "full_hash_files=0" in summaries[0]
        assert "DVD2/VIDEO_TS/VTS_01_0.VOB" in summaries[0]
        assert "DVD3/VIDEO_TS/VTS_01_0.VOB" in summaries[0]
        assert "DVD4/VIDEO_TS/VTS_01_0.VOB" in summaries[0]

        series_root = next((jr / "tv" / "1080p").iterdir())
        season_dir = series_root / "Season 01"
        strms = sorted(season_dir.glob("*.strm"))
        assert len(strms) == len(paths)

        # Both repeated-name groups receive deterministic short-hash + TorrServer file-ID suffixes.
        for file_id, filename in (
            (1, "VTS_01_0.VOB"), (2, "VTS_01_0.VOB"), (3, "VTS_01_0.VOB"),
            (4, "VTS_01_1.VOB"), (5, "VTS_01_1.VOB"),
        ):
            assert any(p.name == f"{filename} [{h[:8]}-{file_id}].strm" for p in strms)
        assert (season_dir / "Unique.mkv.strm").is_file()

        manifest_path = jr / ".torr2strm" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        records = manifest["torrents"][h]["files"]
        assert len(records) == len(paths)
        records_by_id = {record["file_id"]: (rel_path, record) for rel_path, record in records.items()}
        assert set(records_by_id) == set(range(1, len(paths) + 1))
        for file_id, (rel_path, record) in records_by_id.items():
            output_path = jr / rel_path
            assert output_path.is_file()
            assert record["source_path"] == paths[file_id - 1]
            assert f"/play/{h}/{file_id}" in output_path.read_text(encoding="utf-8")
        assert len(list(season_dir.glob("*.nfo"))) == len(paths)
        assert all(len(p.name.encode("utf-8")) <= 255 for p in season_dir.iterdir() if p.is_file())

        before = sorted(p.relative_to(jr).as_posix() for p in jr.rglob("*.strm"))
        assert run(config, client) == 0
        after = sorted(p.relative_to(jr).as_posix() for p in jr.rglob("*.strm"))
        assert before == after
        assert len(after) == len(paths)


def test_sanitize_kodi_component_replaces_windows_reserved_names_and_punctuation():
    safe = sanitize_kodi_component('Baywatch | Extended: "Cut" <Edition> ? *')
    for invalid in '<>:"|?*':
        assert invalid not in safe
    assert "\\" not in safe
    assert sanitize_kodi_component("Folder\\Name") == "Folder - Name"
    assert sanitize_kodi_component("Folder/Season") == "Folder - Season"
    assert sanitize_kodi_component("trailing. ") == "trailing"
    assert sanitize_kodi_component("CON") == "_CON"
    assert sanitize_kodi_component("LPT2.txt") == "_LPT2.txt"


def test_kodi_windows_safe_paths_preserve_extensions_and_do_not_change_jellyfin_names():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h1 = "9" * 40
        movie = torrent(
            h=h1,
            title='Baywatch | Extended: "Cut" <Edition>',
            category="movie",
            paths=['Baywatch | Extended: "Cut".mkv'],
            data={"tmdbid": 339846, "movieTitle": 'Baywatch | Extended: "Cut" <Edition>'},
        )
        p = probe()
        attach_existing_probe(movie, p)

        h2 = "8" * 40
        unknown_tv = torrent(
            h=h2,
            title="Unmatched Series",
            category="tv",
            paths=["Disc|One/Season:01/Episode?01.mkv"],
            data={},
        )
        attach_existing_probe(unknown_tv, p)

        client = FakeClient([movie, unknown_tv], probes={(h1, 1): p, (h2, 1): p})
        assert run(cfg(jr, kr), client) == 0

        kodi_strms = sorted(kr.rglob("*.strm"))
        assert len(kodi_strms) == 2
        for strm in kodi_strms:
            assert strm.suffix == ".strm"
            if strm.relative_to(kr).parts[0] == "tv":
                assert not strm.with_suffix(".nfo").exists()
            else:
                assert strm.with_suffix(".nfo").is_file()
            for component in strm.relative_to(kr).parts:
                for invalid in '<>:"|?*':
                    assert invalid not in component
                assert "\\" not in component
                assert not component.endswith((".", " "))

        relative_parts = next(
            path.relative_to(kr).parts
            for path in kodi_strms
            if "Episode" in path.name
        )
        assert "Disc - One" in relative_parts
        assert "Season - 01" in relative_parts

        jelly_movie_dir = next((jr / "movie" / "1080p").iterdir())
        assert "|" in jelly_movie_dir.name
        jelly_strm = next(jelly_movie_dir.rglob("*.strm"))
        assert "|" in jelly_strm.name

def test_kodi_movie_constructs_magnet_from_torrserver_hash_without_external_lookup():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h = "c" * 40
        t = torrent(h=h, title="Source-only Movie", category="movie", data={})
        client = FakeClient([t])
        with patch("torr2strm.urllib.request.urlopen", side_effect=AssertionError("unexpected HTTP request")):
            assert run(cfg(jr, kr), client) == 0
        kodi_strm = next((kr / "movie" / "1080p").rglob("*.strm"))
        content = kodi_strm.read_text(encoding="utf-8")
        assert "plugin.video.elementum/play?uri=" in content
        assert f"urn%3Abtih%3A{h}" in content
        assert "&oindex=" not in content


def test_kodi_tv_uses_zero_based_original_filestats_order():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr = base / "jelly"
        kr = base / "kodi"
        h = "2" * 40
        t = torrent(
            h=h,
            title="Widow's Bay S01 2026 2160p",
            category="tv",
            paths=["back.jpg", "cover.jpg", "Widow.s.Bay.S01E01.mkv", "Widow.s.Bay.S01E02.mkv"],
            lengths=[100, 200, 500, 500],
        )
        probes = {(h, 3): probe(3840, 2160), (h, 4): probe(3840, 2160)}
        client = FakeClient([t], probes=probes)
        c = cfg(jr, kr)
        assert run(c, client) == 0
        strms = sorted((kr / "tv" / "4K").rglob("*.strm"))
        assert len(strms) == 2
        assert client.ffprobe_calls == []
        contents = {p.read_text() for p in strms}
        assert any("&oindex=2" in x for x in contents)
        assert any("&oindex=3" in x for x in contents)


def test_second_run_reuses_nfo_across_both_outputs():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr = base / "jelly"
        kr = base / "kodi"
        h = "3" * 40
        t = torrent(h=h, title="Film 2026", category="movie")
        payload = probe(1920, 1080)
        attach_existing_probe(t, payload)
        client = FakeClient([t], probes={(h, 1): payload})
        c = cfg(jr, kr)
        assert run(c, client) == 0
        jelly_before = {p.relative_to(jr): p.read_bytes() for p in jr.rglob("*.nfo")}
        kodi_before = {p.relative_to(kr): p.read_bytes() for p in kr.rglob("*.nfo")}
        assert jelly_before and kodi_before
        client.ffprobe_calls.clear()
        assert run(c, client) == 0
        assert client.ffprobe_calls == []
        assert {p.relative_to(jr): p.read_bytes() for p in jr.rglob("*.nfo")} == jelly_before
        assert {p.relative_to(kr): p.read_bytes() for p in kr.rglob("*.nfo")} == kodi_before
        # Both output trees preserve byte-identical NFO files across a no-op resync.


def test_jellyfin_reverse_delete_is_authoritative_and_kodi_is_skipped():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr = base / "jelly"
        kr = base / "kodi"
        h = "4" * 40
        t = torrent(h=h, title="Film", category="movie")
        payload = probe()
        client = FakeClient([t], probes={(h, 1): payload})
        c = cfg(jr, kr, remove=True)
        assert run(c, client) == 0
        jelly_strm = next((jr / "movie" / "1080p").rglob("*.strm"))
        jelly_strm.unlink()
        client.ffprobe_calls.clear()
        assert run(c, client) == 0
        assert client.removed == [h]
        assert not client.torrent_present(h)
        assert not list((jr / "movie" / "1080p").rglob("*.strm"))
        assert not list((kr / "movie" / "1080p").rglob("*.strm"))


def test_kodi_is_read_only_when_strm_deleted():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr = base / "jelly"
        kr = base / "kodi"
        h = "5" * 40
        t = torrent(h=h, title="Film", category="movie")
        payload = probe()
        client = FakeClient([t], probes={(h, 1): payload})
        c = cfg(jr, kr, remove=False)
        assert run(c, client) == 0
        kodi_strm = next((kr / "movie" / "1080p").rglob("*.strm"))
        kodi_strm.unlink()
        assert run(c, client) == 0
        assert client.removed == []
        assert kodi_strm.is_file()


def test_provider_ids_and_base_metadata_are_written_to_nfo():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr = base / "jelly"
        kr = base / "kodi"
        h = "6" * 40
        data = {
            "tmdbId": 1124,
            "imdbId": "tt0482571",
            "tvdbId": 302,
            "tvMazeId": 1234,
            "traktId": 5678,
            "seriesOriginalTitle": "Loki",
            "year": 2021,
            "firstAired": "2021-06-09",
            "episodeAirDate": "2021-06-10",
        }
        t = torrent(h=h, title="Loki S01", category="tv", paths=["Loki.S01E01.mkv"], data=data)
        payload = probe(1920, 872)
        attach_existing_probe(t, payload)
        client = FakeClient([t], probes={(h, 1): payload})
        c = cfg(jr, kr)
        assert run(c, client) == 0
        assert client.ffprobe_calls == []
        nfo = next((jr / "tv" / "1080p").rglob("Loki.S01E01.mkv.nfo"))
        text = nfo.read_text(encoding="utf-8")
        assert "<tmdbid>1124</tmdbid>" in text
        assert "<imdbid>tt0482571</imdbid>" in text
        assert "<tvdbid>302</tvdbid>" in text
        assert '<uniqueid type="tvmaze">1234</uniqueid>' in text
        assert '<uniqueid type="trakt">5678</uniqueid>' in text
        assert "<season>1</season>" in text
        assert "<episode>1</episode>" in text
        assert_no_display_title_fields(text)
        tvshow = next((jr / "tv" / "1080p").rglob("tvshow.nfo"))
        tvshow_text = tvshow.read_text(encoding="utf-8")
        assert_no_display_title_fields(tvshow_text)
        assert "<width>1920</width>" in tvshow_text
        assert "<height>872</height>" in tvshow_text
        assert nfo_format_version(tvshow_text) == 3
        assert tvshow_text.rstrip().endswith("https://www.themoviedb.org/tv/1124")
        episode_text = nfo.read_text(encoding="utf-8")
        assert nfo_format_version(episode_text) == 3
        assert "https://www.themoviedb.org/tv/1124" not in episode_text


def test_manifest_v5_exists_in_both_outputs():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr = base / "jelly"
        kr = base / "kodi"
        h = "7" * 40
        t = torrent(h=h)
        payload = probe()
        client = FakeClient([t], probes={(h, 1): payload})
        c = cfg(jr, kr)
        assert run(c, client) == 0
        for root in (jr, kr):
            manifest = json.loads((root / ".torr2strm" / "manifest.json").read_text())
            assert manifest["version"] == 5
            assert manifest["output"] in {"jellyfin", "kodi"}
            assert manifest["root"] == str(root)


def test_missing_ffprobe_uses_explicit_4k_title_as_root_fallback():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr = base / "jelly"
        kr = base / "kodi"
        h = "8" * 40
        t = torrent(h=h, title="Film 2160p HDR", category="movie")
        client = FakeClient([t], probes={})
        c = cfg(jr, kr)
        assert run(c, client) == 0
        assert list((jr / "movie" / "4K").rglob("*.strm"))
        kodi_strm = next((kr / "movie" / "4K").rglob("*.strm"))
        assert "2160p HDR" in kodi_strm.name
        assert "[88888888]" in kodi_strm.name
        assert client.ffprobe_calls == []


def test_missing_or_unsupported_torrserver_category_stays_uncategorized_without_network_lookup():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h1, h2 = "9" * 40, "a" * 40
        missing = torrent(h=h1, title="Example Series S01", category="", paths=["Example.Series.S01E01.mkv"],
                          data={"category": "tv", "types": ["series"], "quality": "2160p"})
        unsupported = torrent(h=h2, title="Example Anime 2160p", category="anime", paths=["Example.Anime.mkv"],
                              data={"category": "movie", "types": ["movie"], "quality": "2160p"})
        client = FakeClient([missing, unsupported])
        with patch("torr2strm.urllib.request.urlopen", side_effect=AssertionError("unexpected HTTP request")):
            assert run(cfg(jr, kr), client) == 0
        for root in (jr, kr):
            # Unsupported/missing category stays _uncategorized. Both items have
            # recognized structured 2160p metadata, which outranks title/path hints.
            assert len(list((root / "_uncategorized" / "1080p").rglob("*.strm"))) == 0
            assert len(list((root / "_uncategorized" / "4K").rglob("*.strm"))) == 2
            assert not (root / "movie").exists()
            assert not (root / "tv").exists()



def test_metadata_reload_falls_back_to_list_category_for_movie_and_tv():
    cases = [
        ("movie", "Reference Film", "Reference.Film.mkv"),
        ("tv", "Reference Series S01", "Reference.Series.S01E01.mkv"),
    ]
    for index, (category, title, path) in enumerate(cases, start=1):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            jr, kr = base / "jelly", base / "kodi"
            h = f"{index:040x}"
            listed = torrent(h=h, title=title, category=category, paths=[path])
            initial = dict(listed)
            initial["Category"] = ""
            initial["FileStats"] = []
            refreshed = dict(listed)
            refreshed.pop("Category", None)

            client = SequenceResponseClient(listed, [initial, refreshed])
            assert run(cfg(jr, kr), client) == 0

            for root in (jr, kr):
                assert list((root / category / "1080p").rglob("*.strm"))
                assert not (root / "_uncategorized").exists()


def test_refreshed_supported_category_precedes_list_and_unsupported_detail_is_not_overwritten():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h = "b" * 40
        listed = torrent(
            h=h, title="Reference Series S01", category="tv",
            paths=["Reference.Series.S01E01.mkv"],
        )
        initial = dict(listed)
        initial["FileStats"] = []
        refreshed = dict(listed)
        refreshed["Category"] = "movie"

        assert run(
            cfg(jr, kr),
            SequenceResponseClient(listed, [initial, refreshed]),
        ) == 0
        for root in (jr, kr):
            assert list((root / "movie" / "1080p").rglob("*.strm"))
            assert not (root / "tv").exists()
            assert not (root / "_uncategorized").exists()

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h = "c" * 40
        listed = torrent(
            h=h, title="Explicit Unsupported Category", category="tv",
            paths=["Explicit.Unsupported.Category.mkv"],
        )
        detail = dict(listed)
        detail["Category"] = "anime"

        assert run(
            cfg(jr, kr),
            SequenceResponseClient(listed, [detail]),
        ) == 0
        for root in (jr, kr):
            assert list((root / "_uncategorized" / "1080p").rglob("*.strm"))
            assert not (root / "tv").exists()
            assert not (root / "movie").exists()


def test_missing_category_with_prior_trusted_manifest_defers_without_changing_outputs():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h = "d" * 40
        valid = torrent(
            h=h, title="Category Recovery Series S01", category="tv",
            paths=["Category.Recovery.Series.S01E01.mkv"],
        )
        c = cfg(jr, kr)
        assert run(c, FakeClient([valid])) == 0

        roots = (jr, kr)
        def read_manifests():
            return [
                json.loads((root / ".torr2strm" / "manifest.json").read_text(encoding="utf-8"))
                for root in roots
            ]

        def media_files():
            return [
                {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file() and path.suffix.lower() in {".strm", ".nfo"}
                }
                for root in roots
            ]

        before_manifests = read_manifests()
        before_files = media_files()
        assert all(manifest["torrents"][h]["category"] == "tv" for manifest in before_manifests)

        missing_category = dict(valid)
        missing_category["Category"] = ""
        initial_missing = dict(missing_category)
        initial_missing["FileStats"] = []
        with patch.object(torr2strm_module.LOG, "warning") as warning:
            assert run(
                c,
                SequenceResponseClient(missing_category, [initial_missing, missing_category]),
            ) == 0
        assert any("CATEGORY_DEFERRED" in call.args[0] for call in warning.call_args_list)
        assert read_manifests() == before_manifests
        assert media_files() == before_files
        assert all(not (root / "_uncategorized").exists() for root in roots)

        assert run(c, FakeClient([valid])) == 0
        assert read_manifests() == before_manifests
        assert media_files() == before_files
        assert all(not (root / "_uncategorized").exists() for root in roots)

def test_nfo_format_v3_and_combination_tmdb_url_are_generated():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr = base / "jelly"
        kr = base / "kodi"
        h = "c" * 40
        t = torrent(h=h, title="The Prestige 2006", category="movie", data={"tmdbId": 1124, "imdbId": "tt0482571"})
        payload = probe(3840, 2160, codec="hevc")
        attach_existing_probe(t, payload)
        client = FakeClient([t], probes={(h, 1): payload})
        c = cfg(jr, kr)
        assert run(c, client) == 0
        assert client.ffprobe_calls == []
        nfo = next((jr / "movie" / "4K").rglob("*.nfo"))
        text = nfo.read_text(encoding="utf-8")
        assert nfo_format_version(text) == 3
        assert '<torr2strm formatversion="3"' in text
        assert text.rstrip().endswith("https://www.themoviedb.org/movie/1124")
        assert MediaInfoResolver._nfo_is_usable(nfo)


def test_legacy_nfo_is_rewritten_to_allowed_fields_and_gets_tmdb_url():
    legacy = """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<movie>
  <title>The Prestige</title>
  <tmdbid>1124</tmdbid>
  <imdbid>tt0482571</imdbid>
  <tag>USER_NOTE</tag>
  <fileinfo><streamdetails><video><codec>h264</codec><width>1920</width><height>1080</height></video></streamdetails></fileinfo>
</movie>
<!-- generated by torr2strm 1.3.1; media_info_source=nfo -->
"""
    migrated, changed = migrate_nfo_v1_to_v2(legacy)
    assert changed
    assert nfo_format_version(migrated) == 3
    assert "<tag>USER_NOTE</tag>" not in migrated
    assert_no_display_title_fields(migrated)
    assert "<year>" not in migrated
    assert "<fileinfo><streamdetails><video>" in migrated
    assert '<torr2strm formatversion="3"' in migrated
    assert migrated.rstrip().endswith("https://www.themoviedb.org/movie/1124")


def test_normal_sync_migrates_legacy_nfos_without_reprobing():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr = base / "jelly"
        kr = base / "kodi"
        h = "d" * 40
        t = torrent(h=h, title="Film 2026", category="movie", data={"tmdbId": 1124, "imdbId": "tt0482571"})
        payload = probe(1920, 1080)
        attach_existing_probe(t, payload)
        client = FakeClient([t], probes={(h, 1): payload})
        c = cfg(jr, kr)
        assert run(c, client) == 0
        nfos = list(jr.rglob("*.nfo")) + list(kr.rglob("*.nfo"))
        assert nfos
        for path in nfos:
            text = path.read_text(encoding="utf-8")
            xml_end = text.rfind("</movie>") + len("</movie>")
            legacy = text[:xml_end]
            import re
            legacy = re.sub(r"\s*<torr2strm[^>]*/>", "", legacy, count=1)
            legacy += "\n<!-- generated by torr2strm 1.3.1; media_info_source=nfo -->\n"
            path.write_text(legacy, encoding="utf-8")
        client.ffprobe_calls.clear()
        assert run(c, client) == 0
        assert client.ffprobe_calls == []
        for path in nfos:
            text = path.read_text(encoding="utf-8")
            assert nfo_format_version(text) == 3
            assert_no_display_title_fields(text)
            assert text.rstrip().endswith("https://www.themoviedb.org/movie/1124")


def test_nfo_is_identical_between_enabled_outputs_and_contains_hdr_and_streamdetails():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr = base / "jelly"
        kr = base / "kodi"
        h = "b" * 40
        t = torrent(h=h, title="Prestige 2006", category="movie", data={"tmdbId": 1124, "imdbId": "tt0482571", "originalTitle": "The Prestige", "releaseDate": "2006-10-20"})
        p = probe(3840, 2160, codec="hevc", hdr="dv")
        attach_existing_probe(t, p)
        client = FakeClient([t], probes={(h, 1): p})
        c = cfg(jr, kr)
        assert run(c, client) == 0
        assert client.ffprobe_calls == []
        jf_nfo = next((jr / "movie" / "4K").rglob("*.nfo"))
        kd_nfo = next((kr / "movie" / "4K").rglob("*.nfo"))
        assert jf_nfo.read_text(encoding="utf-8") == kd_nfo.read_text(encoding="utf-8")
        text = jf_nfo.read_text(encoding="utf-8")
        assert_no_display_title_fields(text)
        assert "<tmdbid>1124</tmdbid>" in text
        assert "<imdbid>tt0482571</imdbid>" in text
        assert "<hdrtype>dolbyvision</hdrtype>" in text
        assert "<width>3840</width>" in text
        assert "<height>2160</height>" in text
        assert "<fileinfo><streamdetails>" in text


def test_kodi_groups_identified_series_by_id_and_keeps_releases_separate():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h1, h2 = "1" * 40, "2" * 40
        t1 = torrent(h=h1, title="Different.Release.Name.S01E01.720p", category="tv",
                     paths=["S01E01.mkv"], data={"seriesTmdbId": 777, "seriesTitle": "Canonical Series", "seriesYear": 2024})
        t2 = torrent(h=h2, title="Other.Release.Name.S01E01.1080p", category="tv",
                     paths=["Release/episode1.mkv"], data={"seriesTmdbId": 777, "seriesTitle": "Canonical Series", "seriesYear": 2024})
        p720, p1080 = probe(1280, 720), probe(1920, 1080)
        attach_existing_probe(t1, p720)
        attach_existing_probe(t2, p1080)
        client = FakeClient([t1, t2], probes={(h1, 1): p720, (h2, 1): p1080})
        assert run(cfg(jr, kr), client) == 0
        assert client.ffprobe_calls == []
        strms = list((kr / "tv" / "1080p" / "Canonical Series (2024)").rglob("*.strm"))
        assert len(strms) == 2
        assert len({p.name for p in strms}) == 2
        assert any("720p [11111111]" in p.name for p in strms)
        assert any("1080p [22222222]" in p.name for p in strms)
        tvshow_nfo = kr / "tv" / "1080p" / "Canonical Series (2024)" / "tvshow.nfo"
        assert tvshow_nfo.is_file()
        assert "<width>1280</width>" in tvshow_nfo.read_text(encoding="utf-8")
        assert client.ffprobe_calls == []
        assert_no_display_title_fields(tvshow_nfo.read_text(encoding="utf-8"))
        assert not list(kr.rglob("Different.Release.Name*"))
        assert not list(kr.rglob("Other.Release.Name*"))


def test_kodi_unknown_series_preserves_source_hierarchy_and_does_not_invent_episode():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h = "3" * 40
        t = torrent(h=h, title="Mystery.Release.S01.720p", category="tv",
                    paths=["DiscA/Season01/episode-one.mkv"], data={})
        client = FakeClient([t], probes={(h, 1): probe(1280, 720)})
        assert run(cfg(jr, kr), client) == 0
        strm = next((kr / "tv" / "1080p" / "Mystery.Release.S01.720p").rglob("*.strm"))
        assert "DiscA/Season01" in str(strm)
        assert "S01E" not in strm.name
        assert "720p [33333333]" in strm.name
        assert not list((kr / "tv" / "1080p").rglob("tvshow.nfo"))
        assert not list((kr / "tv" / "1080p" / "Mystery.Release.S01.720p").rglob("*.nfo"))


def test_kodi_shared_series_directory_cleanup_preserves_other_release():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h1, h2 = "4" * 40, "5" * 40
        data = {"seriesTmdbId": 991, "seriesTitle": "Shared Show", "seriesYear": 2020}
        t1 = torrent(h=h1, title="Release A S01E01", category="tv", paths=["S01E01.mkv"], data=data)
        t2 = torrent(h=h2, title="Release B S01E02", category="tv", paths=["S01E02.mkv"], data=data)
        p = probe(1920, 1080)
        client = FakeClient([t1, t2], probes={(h1, 1): p, (h2, 1): p})
        c = cfg(jr, kr)
        assert run(c, client) == 0
        first = next((kr / "tv" / "1080p" / "Shared Show (2020)").rglob("*44444444*.strm"))
        client.torrents.pop(h1)
        assert run(c, client) == 0
        assert not first.exists()
        remaining = list((kr / "tv" / "1080p" / "Shared Show (2020)").rglob("*.strm"))
        assert len(remaining) == 1
        assert "[55555555]" in remaining[0].name
        assert (kr / "tv" / "1080p" / "Shared Show (2020)" / "tvshow.nfo").is_file()


def test_structured_lower_resolution_overrides_explicit_4k_title_fallback():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h = "e" * 40
        t = torrent(h=h, title="Film.2160p.HDR", category="movie", data={"quality": "720p"})
        client = FakeClient([t], probes={})
        assert run(cfg(jr, kr), client) == 0
        kodi_strm = next((kr / "movie" / "1080p").rglob("*.strm"))
        assert "720p" in kodi_strm.name
        assert "2160p" not in kodi_strm.name
        assert client.ffprobe_calls == []
        assert "HDR" not in kodi_strm.name


def test_usable_non_4k_ffprobe_dimensions_override_4k_title_marker():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h = "d" * 40
        t = torrent(h=h, title="Film.2160p.HDR", category="movie")
        p = probe(1920, 1080)
        attach_existing_probe(t, p)
        client = FakeClient([t], probes={(h, 1): p})
        assert run(cfg(jr, kr), client) == 0
        assert list((jr / "movie" / "1080p").rglob("*.strm"))
        assert list((kr / "movie" / "1080p").rglob("*.strm"))
        assert not list((kr / "movie" / "4K").rglob("*.strm"))
        assert client.ffprobe_calls == []


def test_unknown_quality_uses_1080p_root_without_quality_suffix():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h = "f" * 40
        t = torrent(h=h, title="Unclassified Movie Release", category="movie", data={})
        client = FakeClient([t], probes={})
        assert run(cfg(jr, kr), client) == 0
        kodi_strm = next((kr / "movie" / "1080p").rglob("*.strm"))
        assert " — " not in kodi_strm.name
        assert "[ffffffff]" in kodi_strm.name


def test_no_episode_ffprobe_calls_without_existing_per_file_probe_data():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h = "a" * 40
        t = torrent(
            h=h, title="Season Pack S01", category="tv",
            paths=["Episode.S01E01.mkv", "Episode.S01E02.mkv"],
            lengths=[1000, 900], data={"seriesTmdbId": 42, "seriesTitle": "Season Pack"},
        )
        primary, secondary = probe(3840, 2160), probe(1280, 720)
        attach_existing_probe(t, primary)
        client = FakeClient([t], probes={(h, 1): primary, (h, 2): secondary})
        assert run(cfg(jr, kr), client) == 0
        assert client.ffprobe_calls == []
        season_dir = kr / "tv" / "4K" / "Season Pack" / "Season 01"
        assert len(list(season_dir.rglob("*.strm"))) == 2
        assert not list(season_dir.rglob("*.nfo"))
        tvshow_nfo = kr / "tv" / "4K" / "Season Pack" / "tvshow.nfo"
        assert tvshow_nfo.is_file()
        tvshow_text = tvshow_nfo.read_text(encoding="utf-8")
        assert "<width>3840</width>" in tvshow_text
        assert_no_display_title_fields(tvshow_text)


def test_known_series_files_without_episode_coordinates_do_not_get_duplicate_file_suffix():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h = "9" * 40
        t = torrent(
            h=h, title="Canonical Series Pack S01", category="tv",
            paths=["Disc/part-a.mkv", "Disc/part-b.mkv"],
            lengths=[1000, 900],
            data={"seriesTmdbId": 902, "seriesTitle": "Canonical Series"},
        )
        p = probe(1920, 1080)
        client = FakeClient([t], probes={(h, 1): p, (h, 2): p})
        assert run(cfg(jr, kr), client) == 0
        names = sorted(p.name for p in (kr / "tv" / "1080p" / "Canonical Series" / "Season 01").rglob("*.strm"))
        assert len(names) == 2
        assert all("[file" not in name for name in names)


def test_duplicate_episode_in_same_torrent_gets_original_file_ordinal_only_for_collision():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h = "8" * 40
        t = torrent(
            h=h, title="Canonical Series S01", category="tv",
            paths=["S01E01-copyA.mkv", "S01E01-copyB.mkv", "S01E02.mkv"],
            lengths=[1000, 900, 800],
            data={"seriesTmdbId": 903, "seriesTitle": "Canonical Series"},
        )
        p = probe(1920, 1080)
        client = FakeClient([t], probes={(h, 1): p, (h, 2): p, (h, 3): p})
        assert run(cfg(jr, kr), client) == 0
        names = sorted(p.name for p in (kr / "tv" / "1080p" / "Canonical Series" / "Season 01").rglob("*.strm"))
        assert len(names) == 3
        assert sum("[file" in name for name in names) == 2
        assert any("S01E02" in name and "[file" not in name for name in names)


def test_bounded_kodi_leaf_preserves_quality_and_hash_when_title_is_too_long():
    from torr2strm import bounded_kodi_leaf

    leaf = bounded_kodi_leaf("ОченьДлинноеНазвание" * 30, "720p HDR", "a1b2c3d4")
    assert len((leaf + ".strm").encode("utf-8")) <= 255
    assert len((leaf + ".nfo").encode("utf-8")) <= 255
    assert leaf.endswith(" — 720p HDR [a1b2c3d4]")

    duplicate = bounded_kodi_leaf("ОченьДлинноеНазвание" * 30, "720p", "a1b2c3d4", file_ordinal=3)
    assert len((duplicate + ".strm").encode("utf-8")) <= 255
    assert duplicate.endswith(" — 720p [a1b2c3d4] [file03]")


def test_bounded_kodi_component_preserves_short_names_and_uses_stable_utf8_suffixes():
    assert bounded_kodi_component("Short Name", identity="a" * 40) == "Short Name"

    long_name = "Престиж - " + ("ОченьДлинноеНазвание " * 30)
    first = bounded_kodi_component(long_name, identity="a" * 40)
    second = bounded_kodi_component(long_name, identity="b" * 40)
    assert len(first.encode("utf-8")) <= 255
    assert len(second.encode("utf-8")) <= 255
    assert first.endswith("[aaaaaaaa]")
    assert second.endswith("[bbbbbbbb]")
    assert first != second
    assert first.encode("utf-8").decode("utf-8") == first
    assert not first.endswith((".", " "))

    provider_suffix = bounded_kodi_component(long_name, identity="tmdb:123", suffix="tmdb-123")
    assert len(provider_suffix.encode("utf-8")) <= 255
    assert provider_suffix.endswith("[tmdb-123]")


def test_kodi_long_movie_directories_are_bounded_collision_safe_and_stable():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        long_title = "Престиж - The Prestige - " + ("ОченьДлинноеНазвание " * 28)
        t1 = torrent(
            h="a" * 40,
            title="Prestige.2006.2160p.WEB-DL",
            category="movie",
            paths=["Prestige.mkv"],
            data={"tmdbId": 1001, "movieTitle": long_title, "year": 2006},
        )
        t2 = torrent(
            h="b" * 40,
            title="Prestige.2006.2160p.WEB-DL",
            category="movie",
            paths=["Prestige.mkv"],
            data={"tmdbId": 1002, "movieTitle": long_title, "year": 2006},
        )
        p = probe(3840, 2160)
        attach_existing_probe(t1, p)
        attach_existing_probe(t2, p)
        client = FakeClient([t1, t2])
        config = cfg(jr, kr)
        config["outputs"]["jellyfin"]["enabled"] = False

        assert run(config, client) == 0
        root = kr / "movie" / "4K"
        dirs = sorted(p for p in root.iterdir() if p.is_dir())
        assert len(dirs) == 2
        assert dirs[0].name != dirs[1].name
        assert any(p.name.endswith("[tmdb-1001]") for p in dirs)
        assert any(p.name.endswith("[tmdb-1002]") for p in dirs)

        for path in kr.rglob("*"):
            for component in path.relative_to(kr).parts:
                assert len(component.encode("utf-8")) <= 255, (path, component)
                for invalid in '<>:"|?*':
                    assert invalid not in component
                assert "\\" not in component
                assert not component.endswith((".", " "))

        before = sorted(str(p.relative_to(kr)) for p in kr.rglob("*.strm"))
        assert len(before) == 2
        assert len(list(kr.rglob("*.nfo"))) == 2
        assert run(config, client) == 0
        after = sorted(str(p.relative_to(kr)) for p in kr.rglob("*.strm"))
        assert before == after


def test_kodi_unknown_torrent_bounds_nested_source_directories_and_resolves_truncation_collisions():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        prefix = "ОченьДлинныйКаталог" * 18
        folder_a = prefix + "A"
        folder_b = prefix + "B"
        t = torrent(
            h="c" * 40,
            title="Unknown nested TV release",
            category="tv",
            paths=[f"{folder_a}/EpisodeA.mkv", f"{folder_b}/EpisodeB.mkv"],
            lengths=[1000, 900],
            data={},
        )
        client = FakeClient([t])
        config = cfg(jr, kr)
        config["outputs"]["jellyfin"]["enabled"] = False

        assert run(config, client) == 0
        strms = sorted(kr.rglob("*.strm"))
        assert len(strms) == 2
        parent_names = {p.parent.name for p in strms}
        assert len(parent_names) == 2
        for path in strms:
            for component in path.relative_to(kr).parts:
                assert len(component.encode("utf-8")) <= 255, (path, component)
                for invalid in '<>:"|?*':
                    assert invalid not in component
                assert "\\" not in component
                assert not component.endswith((".", " "))


def test_kodi_different_series_ids_with_same_name_and_year_get_id_suffixes():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h1, h2 = "b1" * 20, "b2" * 20
        t1 = torrent(h=h1, title="Same.Show.S01E01", category="tv", paths=["S01E01.mkv"],
                     data={"seriesTvdbId": 111, "seriesTitle": "Same Show", "seriesYear": 2024})
        t2 = torrent(h=h2, title="Same.Show.S01E01", category="tv", paths=["S01E01.mkv"],
                     data={"seriesTvdbId": 222, "seriesTitle": "Same Show", "seriesYear": 2024})
        p = probe(1920, 1080)
        client = FakeClient([t1, t2], probes={(h1, 1): p, (h2, 1): p})
        assert run(cfg(jr, kr), client) == 0
        dirs = sorted(p.name for p in (kr / "tv" / "1080p").iterdir() if p.is_dir())
        assert dirs == ["Same Show (2024) [tvdb-111]", "Same Show (2024) [tvdb-222]"]


def test_kodi_movie_versions_share_movie_directory_and_remain_distinct():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h1, h2 = "c1" * 20, "c2" * 20
        data = {"tmdbId": 157336, "movieTitle": "Interstellar", "year": 2014}
        t1 = torrent(h=h1, title="Interstellar.720p", category="movie", data={**data, "quality": "720p"})
        t2 = torrent(h=h2, title="Interstellar.1080p", category="movie", data={**data, "quality": "1080p"})
        client = FakeClient([t1, t2], probes={})
        assert run(cfg(jr, kr), client) == 0
        root = kr / "movie" / "1080p" / "Interstellar (2014)"
        strms = sorted(root.glob("*.strm"))
        assert len(strms) == 2
        assert any("720p [c1c1c1c1]" in p.name for p in strms)
        assert any("1080p [c2c2c2c2]" in p.name for p in strms)
        assert all("plugin.video.elementum/play?uri=" in p.read_text() for p in strms)
        assert all("&oindex=" not in p.read_text() for p in strms)


def test_kodi_short_hash_is_extended_only_when_first_eight_characters_collide():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        prefix = "12345678"
        h1, h2 = prefix + "a" * 32, prefix + "b" * 32
        t1 = torrent(h=h1, title="Collision.Show.S01E01", category="tv", paths=["S01E01.mkv"],
                     data={"seriesTmdbId": 7001, "seriesTitle": "Collision Show"})
        t2 = torrent(h=h2, title="Collision.Show.S01E02", category="tv", paths=["S01E02.mkv"],
                     data={"seriesTmdbId": 7001, "seriesTitle": "Collision Show"})
        p = probe(1920, 1080)
        client = FakeClient([t1, t2], probes={(h1, 1): p, (h2, 1): p})
        assert run(cfg(jr, kr), client) == 0
        names = sorted(p.name for p in (kr / "tv" / "1080p" / "Collision Show" / "Season 01").glob("*.strm"))
        assert len(names) == 2
        assert any("[12345678a]" in name for name in names)
        assert any("[12345678b]" in name for name in names)


def test_quality_root_uses_usable_dimensions_before_explicit_title_fallback():
    assert quality_root_from_probe_payload(None) == "1080p"
    assert quality_root_from_probe_payload({"quality": "2160p"}) == "1080p"
    assert quality_root_from_probe_payload(probe(1920, 1080)) == "1080p"
    assert quality_root_from_probe_payload(probe(2560, 1440)) == "1080p"
    assert quality_root_from_probe_payload(probe(3840, 2160, hdr="dv")) == "4K"
    assert quality_root_from_probe_or_title(None, "Film 2160p HDR") == "4K"
    assert quality_root_from_probe_or_title(None, "Film 4K UHD") == "4K"
    assert quality_root_from_probe_or_title(None, "Film UHD") == "1080p"
    assert quality_root_from_probe_or_title(None, "Film 1440p") == "1080p"
    assert quality_root_from_probe_or_title(probe(1920, 1080), "Film 2160p") == "1080p"
    assert quality_root_from_probe_or_title(probe(3840, 2160), "Film 1080p") == "4K"



def test_quality_root_evidence_precedence_includes_structured_metadata_and_file_paths():
    assert quality_root_from_probe_or_title(
        probe(1920, 1080), "Film 2160p", {"quality": "2160p"}, ["release 4K.mkv"]
    ) == "1080p"
    assert quality_root_from_probe_or_title(
        probe(3840, 2160), "Film 1080p", {"quality": "1080p"}, []
    ) == "4K"
    assert quality_root_from_probe_or_title(
        None, "Film 2160p", {"resolution": "1080"}, ["folder/Film 4K.mkv"]
    ) == "1080p"
    assert quality_root_from_probe_or_title(None, "Film", {"quality": "2160"}, []) == "4K"
    assert quality_root_from_probe_or_title(
        None, "Release without a marker", {}, ["season/Film_2160p_WEB-DL.mkv"]
    ) == "4K"
    assert quality_root_from_probe_or_title(None, "Film 2160pixels", {}, []) == "1080p"
    assert quality_root_from_probe_or_title(None, "Film", {}, ["Film.2160pish.mkv"]) == "1080p"
    assert quality_root_from_probe_or_title(None, "Film without quality", {}, []) == "1080p"

def test_torr2strm_does_not_request_ffprobe_from_torrserver():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h = "7" * 40
        t = torrent(h=h, title="Movie without resolution", category="movie", data={})
        client = FakeClient([t], probes={(h, 1): probe(3840, 2160)})
        assert run(cfg(jr, kr), client) == 0
        assert client.ffprobe_calls == []
        assert list((jr / "movie" / "1080p").rglob("*.strm"))
        assert not list((jr / "movie" / "4K").rglob("*.strm"))



def test_deferred_torrent_preserves_previous_trees_while_healthy_items_continue():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        bad_hash, healthy_hash = "a" * 40, "b" * 40
        bad = torrent(h=bad_hash, title="Temporarily unreadable film", data={})
        healthy = torrent(h=healthy_hash, title="Healthy film", data={})
        assert run(cfg(jr, kr), FakeClient([bad, healthy])) == 0

        jelly_manifest = jr / ".torr2strm" / "manifest.json"
        kodi_manifest = kr / ".torr2strm" / "manifest.json"
        before_jelly = json.loads(jelly_manifest.read_text(encoding="utf-8"))["torrents"]
        before_kodi = json.loads(kodi_manifest.read_text(encoding="utf-8"))["torrents"]
        bad_jelly_record, bad_kodi_record = before_jelly[bad_hash], before_kodi[bad_hash]
        bad_jelly_files = {rel: (jr / rel).read_bytes() for rel in bad_jelly_record["files"]}
        bad_kodi_files = {rel: (kr / rel).read_bytes() for rel in bad_kodi_record["files"]}

        bad_current = torrent(h=bad_hash, title="Temporarily unreadable film", data={})
        healthy_current = torrent(h=healthy_hash, title="Healthy film — updated", data={})
        class OneTorrentUnavailable(FakeClient):
            def get_torrent(self, h):
                if h == bad_hash:
                    raise RuntimeError("temporary TorrServer detail timeout")
                return super().get_torrent(h)

        assert run(cfg(jr, kr), OneTorrentUnavailable([bad_current, healthy_current])) == 0
        after_jelly = json.loads(jelly_manifest.read_text(encoding="utf-8"))["torrents"]
        after_kodi = json.loads(kodi_manifest.read_text(encoding="utf-8"))["torrents"]
        assert after_jelly[bad_hash] == bad_jelly_record
        assert after_kodi[bad_hash] == bad_kodi_record
        for rel, content in bad_jelly_files.items():
            assert (jr / rel).read_bytes() == content
        for rel, content in bad_kodi_files.items():
            assert (kr / rel).read_bytes() == content
        assert after_jelly[healthy_hash]["directory"] != before_jelly[healthy_hash]["directory"]
        assert after_kodi[healthy_hash]["directory"] != before_kodi[healthy_hash]["directory"]


def test_apprise_notifications_use_shared_contract_and_suppress_unchanged_events():
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        cfg_data = cfg(base / "jelly", base / "kodi")
        cfg_data["notifications"].update({
            "enabled": True,
            "apprise_url": "http://192.168.0.14:8000",
            "apprise_config_key": "arr-stack",
            "service_name": "torr2strm / MINI-Ubuntu",
            "state_path": str(base / "notifications.json"),
        })
        capture = NotificationCapture()
        warning = logging.LogRecord(
            "torr2strm", logging.WARNING, __file__, 1,
            "TORRENT_DEFERRED hash=%s stage=%s reason=%s retry=next_run",
            ("a" * 40, "metadata_wait", "FileStats temporarily unavailable"), None,
        )
        error = logging.LogRecord(
            "torr2strm", logging.ERROR, __file__, 2,
            "ERROR output=%s hash=%s operation=%s",
            ("jellyfin", "b" * 40, "sync"), None,
        )
        collision_summary = logging.LogRecord(
            "torr2strm", logging.WARNING, __file__, 3,
            "STRM_COLLISION_SUMMARY output=%s title=%r hash=%s affected_files=%d short_hash_files=%d full_hash_files=%d examples=%s",
            (
                "jellyfin", "Lexx (1997) Season 01 DVD collection", "a" * 40, 22, 22, 0,
                "file_id=72:Season 1/DVD1/VTS_01_0.VOB;file_id=82:Season 1/DVD2/VTS_01_0.VOB;file_id=91:Season 1/DVD3/VTS_01_0.VOB",
            ), None,
        )
        collision_detail = logging.LogRecord(
            "torr2strm", logging.WARNING, __file__, 4,
            "STRM_COLLISION_RESOLVED output=%s hash=%s file_id=%d source=%s suffix=%s",
            ("jellyfin", "a" * 40, 72, "Season 1/DVD1/VTS_01_0.VOB", "aaaaaaaa-72"), None,
        )
        capture.emit(warning)
        capture.emit(error)
        capture.emit(collision_summary)
        capture.emit(collision_detail)
        with patch.object(torr2strm_module.urllib.request, "urlopen", return_value=Response()) as urlopen:
            dispatch_run_notifications(cfg_data, capture)
            assert urlopen.call_count == 2
            requests = [call.args[0] for call in urlopen.call_args_list]
            payloads = [json.loads(req.data.decode("utf-8")) for req in requests]
            assert all(req.full_url == "http://192.168.0.14:8000/notify/arr-stack" for req in requests)
            warning_payload = next(p for p in payloads if p["type"] == "warning")
            error_payload = next(p for p in payloads if p["type"] == "failure")
            assert warning_payload["tag"] == "warnings"
            assert "TorrServer пока не предоставил необходимые сведения" in warning_payload["body"]
            assert "TORRENT_DEFERRED" not in warning_payload["body"]
            assert "STRM_COLLISION_SUMMARY" not in warning_payload["body"]
            assert "Lexx (1997) Season 01 DVD collection" in warning_payload["body"]
            assert "У 22 файлов совпадали целевые имена" in warning_payload["body"]
            assert "Season 1/DVD2/VTS_01_0.VOB" in warning_payload["body"]
            assert "Season 1/DVD3/VTS_01_0.VOB" in warning_payload["body"]
            assert "STRM_COLLISION_RESOLVED" not in warning_payload["body"]
            assert error_payload["tag"] == "critical"
            assert "Не удалось синхронизировать медиа" in error_payload["body"]
            assert "Раздача bbbbbbbb" in error_payload["body"]
            dispatch_run_notifications(cfg_data, capture)
            assert urlopen.call_count == 2
            dispatch_run_notifications(cfg_data, NotificationCapture())
            capture_again = NotificationCapture()
            capture_again.emit(warning)
            dispatch_run_notifications(cfg_data, capture_again)
            assert urlopen.call_count == 3



def test_nfo_application_version_does_not_rewrite_existing_sidecars():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h = "d" * 40
        t = torrent(h=h, title="The Prestige 2006", category="movie",
                    data={"tmdbId": 1124, "imdbId": "tt0482571"})
        attach_existing_probe(t, probe(1920, 1080))
        client = FakeClient([t])
        c = cfg(jr, kr)
        assert run(c, client) == 0
        nfos = list(jr.rglob("*.nfo")) + list(kr.rglob("*.nfo"))
        assert nfos
        mtimes = {}
        expected_text = {}
        for path in nfos:
            text = path.read_text(encoding="utf-8")
            assert "generated by torr2strm;" in text
            assert "generated by torr2strm 1.4.11;" not in text
            # Model an already-installed NFO from v1.4.10. Removing the
            # application version from the new header must not rewrite it.
            legacy = text.replace(
                "generated by torr2strm;",
                "generated by torr2strm 1.4.10;",
                1,
            )
            path.write_text(legacy, encoding="utf-8")
            expected_text[path] = legacy
            mtimes[path] = path.stat().st_mtime_ns

        with patch.object(torr2strm_module, "VERSION", "99.0.0"):
            assert run(c, client) == 0

        for path in nfos:
            assert path.read_text(encoding="utf-8") == expected_text[path]
            assert path.stat().st_mtime_ns == mtimes[path]


def test_intentional_nfo_format_version_bump_rewrites_sidecars():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h = "e" * 40
        t = torrent(h=h, title="The Prestige 2006", category="movie",
                    data={"tmdbId": 1124, "imdbId": "tt0482571"})
        attach_existing_probe(t, probe(1920, 1080))
        client = FakeClient([t])
        c = cfg(jr, kr)
        assert run(c, client) == 0
        nfos = list(jr.rglob("*.nfo")) + list(kr.rglob("*.nfo"))
        assert nfos
        assert all(nfo_format_version(p.read_text(encoding="utf-8")) == 3 for p in nfos)

        with patch.object(torr2strm_module, "NFO_FORMAT_VERSION", 4):
            assert run(c, client) == 0
            assert all(nfo_format_version(p.read_text(encoding="utf-8")) == 4 for p in nfos)
            assert all('<torr2strm formatversion="4"' in p.read_text(encoding="utf-8") for p in nfos)


def test_kodi_canonical_series_name_and_earliest_year_span_quality_roots():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h2022, h2021 = "2" * 40, "1" * 40
        t2022 = torrent(
            h=h2022, title="The White Lotus S01E02 2022", category="tv",
            paths=["The.White.Lotus.S01E02.mkv"],
            data={"seriesTmdbId": 777, "seriesTitle": "The White Lotus", "seriesYear": 2022},
        )
        t2021 = torrent(
            h=h2021, title="The White Lotus S01E01 2021", category="tv",
            paths=["The.White.Lotus.S01E01.mkv"],
            data={"seriesTmdbId": 777, "seriesTitle": "The White Lotus", "seriesYear": 2021},
        )
        attach_existing_probe(t2022, probe(1920, 1080))
        attach_existing_probe(t2021, probe(3840, 2160, codec="hevc"))
        # Put the later-year release first to test deterministic canonical naming.
        assert run(cfg(jr, kr), FakeClient([t2022, t2021])) == 0
        for quality_root in ("4K", "1080p"):
            canonical = kr / "tv" / quality_root / "The White Lotus (2021)"
            assert canonical.is_dir()
            assert list(canonical.rglob("*.strm"))
            assert not (kr / "tv" / quality_root / "The White Lotus (2022)").exists()


def test_quality_resolution_failure_preserves_outputs_until_recovery_or_disappearance():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h2021, h2022 = "1" * 40, "2" * 40
        t2021 = torrent(
            h=h2021, title="Canonical Series S01E01 2021", category="tv",
            paths=["Canonical.Series.S01E01.mkv"],
            data={"seriesTmdbId": 777, "seriesTitle": "Canonical Series", "seriesYear": 2021},
        )
        t2022 = torrent(
            h=h2022, title="Canonical Series S01E02 2022", category="tv",
            paths=["Canonical.Series.S01E02.mkv"],
            data={"seriesTmdbId": 777, "seriesTitle": "Canonical Series", "seriesYear": 2022},
        )
        attach_existing_probe(t2021, probe(3840, 2160, codec="hevc"))
        attach_existing_probe(t2022, probe(1920, 1080))
        client = FakeClient([t2021, t2022])
        c = cfg(jr, kr)
        assert run(c, client) == 0

        jelly_manifest = jr / ".torr2strm" / "manifest.json"
        kodi_manifest = kr / ".torr2strm" / "manifest.json"
        old_jelly = json.loads(jelly_manifest.read_text(encoding="utf-8"))["torrents"]
        old_kodi = json.loads(kodi_manifest.read_text(encoding="utf-8"))["torrents"]
        record_jelly, record_kodi = old_jelly[h2021], old_kodi[h2021]
        files_jelly = {rel: (jr / rel).read_bytes() for rel in record_jelly["files"]}
        files_kodi = {rel: (kr / rel).read_bytes() for rel in record_kodi["files"]}

        original_prepare = MediaInfoResolver.prepare
        def fail_one_release(self, snapshot, required):
            if snapshot.hash == h2021:
                raise RuntimeError("temporary quality metadata failure")
            return original_prepare(self, snapshot, required)

        with patch.object(MediaInfoResolver, "prepare", new=fail_one_release):
            assert run(c, client) == 0

        during_jelly = json.loads(jelly_manifest.read_text(encoding="utf-8"))["torrents"]
        during_kodi = json.loads(kodi_manifest.read_text(encoding="utf-8"))["torrents"]
        assert during_jelly[h2021] == record_jelly
        assert during_kodi[h2021] == record_kodi
        for rel, content in files_jelly.items():
            assert (jr / rel).read_bytes() == content
        for rel, content in files_kodi.items():
            assert (kr / rel).read_bytes() == content
        assert (kr / "tv" / "1080p" / "Canonical Series (2021)").is_dir()

        # Once quality resolution succeeds again, normal synchronization resumes.
        assert run(c, client) == 0
        recovered_jelly = json.loads(jelly_manifest.read_text(encoding="utf-8"))["torrents"]
        recovered_kodi = json.loads(kodi_manifest.read_text(encoding="utf-8"))["torrents"]
        assert h2021 in recovered_jelly and h2021 in recovered_kodi
        assert (kr / "tv" / "4K" / "Canonical Series (2021)").is_dir()
        assert not (kr / "tv" / "4K" / "Canonical Series (2022)").exists()

        # A true source disappearance is still cleaned up at the next sync.
        client.torrents.pop(h2021)
        assert run(c, client) == 0
        after_jelly = json.loads(jelly_manifest.read_text(encoding="utf-8"))["torrents"]
        after_kodi = json.loads(kodi_manifest.read_text(encoding="utf-8"))["torrents"]
        assert h2021 not in after_jelly and h2021 not in after_kodi
        for rel in files_jelly:
            assert not (jr / rel).exists()
        for rel in files_kodi:
            assert not (kr / rel).exists()
        assert h2022 in after_jelly and h2022 in after_kodi


def test_permanent_no_video_skip_cleans_old_managed_outputs():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h = "f" * 40
        t = torrent(h=h, title="Film without video", category="movie",
                    data={"tmdbId": 1124})
        attach_existing_probe(t, probe(1920, 1080))
        client = FakeClient([t])
        c = cfg(jr, kr)
        assert run(c, client) == 0
        manifests = {
            root: root / ".torr2strm" / "manifest.json"
            for root in (jr, kr)
        }
        before = {
            root: json.loads(manifest.read_text(encoding="utf-8"))["torrents"][h]["files"]
            for root, manifest in manifests.items()
        }
        assert all((root / next(iter(files))).exists() for root, files in before.items())

        client.torrents[h] = torrent(
            h=h, title="Film without video", category="movie",
            paths=["notes.txt"], data={"tmdbId": 1124},
        )
        assert run(c, client) == 0
        for root, manifest in manifests.items():
            records = json.loads(manifest.read_text(encoding="utf-8"))["torrents"]
            assert h not in records
            assert all(not (root / rel).exists() for rel in before[root])
            assert not list(root.rglob("*.strm"))


def _build_permission_test_runner(base: Path):
    jr, kr = base / "jelly", base / "kodi"
    coordinator = SyncCoordinator(cfg(jr, kr), FakeClient([]), dry_run=False)
    runner = next(output for output in coordinator.outputs if output.spec.name == "jellyfin")
    root = runner.root
    root.mkdir(parents=True, exist_ok=True)
    manifest_dir = runner.manifest_path.parent
    manifest_dir.mkdir(parents=True, exist_ok=True)
    managed_dir = root / "tv" / "1080p"
    managed_dir.mkdir(parents=True, exist_ok=True)
    strm = managed_dir / "episode.strm"
    nfo = managed_dir / "episode.nfo"
    strm.write_text("http://127.0.0.1:8092/play/example/1\n", encoding="utf-8")
    nfo.write_text("<movie />\n", encoding="utf-8")
    return runner, root, manifest_dir, managed_dir, strm, nfo


def test_no_chmod_when_existing_output_modes_are_already_correct():
    with tempfile.TemporaryDirectory() as td:
        runner, root, manifest_dir, managed_dir, strm, nfo = _build_permission_test_runner(Path(td))
        real_chmod = os.chmod
        for path, mode in (
            (root, 0o777), (manifest_dir, 0o777), (managed_dir, 0o777),
            (strm, 0o666), (nfo, 0o666),
        ):
            real_chmod(path, mode)

        with patch("torr2strm.os.chmod", wraps=real_chmod) as chmod:
            runner.prepare_root()
            runner._mkdir(managed_dir)
            runner._write_strm(strm, strm.read_text(encoding="utf-8"), "a" * 40,
                               "episode.mkv", "tv/1080p/episode.strm")
            runner._write_nfo(nfo, nfo.read_text(encoding="utf-8"), "a" * 40,
                              "episode.mkv", "tv/1080p/episode.nfo")
            chmod.assert_not_called()


def test_chmod_repairs_modes_only_when_they_differ():
    with tempfile.TemporaryDirectory() as td:
        runner, root, manifest_dir, managed_dir, strm, nfo = _build_permission_test_runner(Path(td))
        real_chmod = os.chmod
        for path in (root, manifest_dir, managed_dir):
            real_chmod(path, 0o700)
        for path in (strm, nfo):
            real_chmod(path, 0o600)

        with patch("torr2strm.os.chmod", wraps=real_chmod) as chmod:
            runner.prepare_root()
            runner._mkdir(managed_dir)
            runner._write_strm(strm, strm.read_text(encoding="utf-8"), "a" * 40,
                               "episode.mkv", "tv/1080p/episode.strm")
            runner._write_nfo(nfo, nfo.read_text(encoding="utf-8"), "a" * 40,
                              "episode.mkv", "tv/1080p/episode.nfo")

        actual = {Path(call.args[0]): call.args[1] for call in chmod.call_args_list}
        assert actual == {
            root: 0o777, manifest_dir: 0o777, managed_dir: 0o777,
            strm: 0o666, nfo: 0o666,
        }
        assert stat.S_IMODE(root.stat().st_mode) == 0o777
        assert stat.S_IMODE(manifest_dir.stat().st_mode) == 0o777
        assert stat.S_IMODE(managed_dir.stat().st_mode) == 0o777
        assert stat.S_IMODE(strm.stat().st_mode) == 0o666
        assert stat.S_IMODE(nfo.stat().st_mode) == 0o666



def test_kodi_movie_names_remain_scoped_to_each_quality_root():
    """Series canonicalization must not rename identified movie output folders."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h1080, h4k = "a" * 40, "b" * 40
        t1080 = torrent(
            h=h1080,
            title="Alpha Film WEB-DL 2001",
            category="movie",
            data={"tmdbId": 1124, "movieTitle": "Alpha Film", "year": 2001},
        )
        t4k = torrent(
            h=h4k,
            title="Zebra Film UHD 2002",
            category="movie",
            data={"tmdbId": 1124, "movieTitle": "Zebra Film", "year": 2002},
        )
        attach_existing_probe(t1080, probe(1920, 1080))
        attach_existing_probe(t4k, probe(3840, 2160, codec="hevc"))

        assert run(cfg(jr, kr), FakeClient([t4k, t1080])) == 0

        assert (kr / "movie" / "1080p" / "Alpha Film (2001)").is_dir()
        assert (kr / "movie" / "4K" / "Zebra Film (2002)").is_dir()
        assert not (kr / "movie" / "1080p" / "Zebra Film (2002)").exists()
        assert not (kr / "movie" / "4K" / "Alpha Film (2001)").exists()


def test_apprise_groups_torrent_failures_by_problem_and_media_and_explains_hash_only_404():
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        cfg_data = cfg(base / "jelly", base / "kodi")
        cfg_data["notifications"].update({
            "enabled": True,
            "apprise_url": "http://192.168.0.14:8000",
            "apprise_config_key": "arr-stack",
            "service_name": "torr2strm / MINI-Ubuntu",
            "state_path": str(base / "notifications.json"),
        })
        capture = NotificationCapture()
        missing_hash = "a" * 40
        metadata_hash = "b" * 40
        capture.register_title(missing_hash, "Definitely, Maybe (2008)")
        capture.register_title(metadata_hash, "The Second Film (2024)")
        capture.warnings.extend([
            f"TORRENT_DEFERRED hash={missing_hash} stage=action_get reason={missing_hash} retry=next_run",
            f"TORRENT_DEFERRED hash={metadata_hash} stage=metadata_wait reason=FileStats temporarily unavailable retry=next_run",
        ])

        with patch.object(torr2strm_module.urllib.request, "urlopen", return_value=Response()) as urlopen:
            dispatch_run_notifications(cfg_data, capture)
            assert urlopen.call_count == 1
            payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
            body = payload["body"]
            assert payload["tag"] == "warnings"
            assert "Временно не удалось получить данные о раздачах" in body
            assert body.count("Временно не удалось получить данные о раздачах") == 1
            assert "Definitely, Maybe (2008)" in body
            assert "The Second Film (2024)" in body
            assert "HTTP 404" in body
            assert "следующего запуска" in body.lower()
            assert missing_hash not in body
            assert metadata_hash not in body
            assert "TORRENT_DEFERRED" not in body


def test_apprise_does_not_forward_successful_cleanup_and_renders_exception_details():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        cfg_data = cfg(base / "jelly", base / "kodi")
        cfg_data["notifications"].update({
            "enabled": True,
            "apprise_url": "http://192.168.0.14:8000",
            "apprise_config_key": "arr-stack",
            "service_name": "torr2strm / MINI-Ubuntu",
            "state_path": str(base / "notifications.json"),
        })
        capture = NotificationCapture()
        cleanup = logging.LogRecord(
            "torr2strm", logging.WARNING, __file__, 1,
            "REMOVE-TREE output=%s hash=%s path=%s reason=%s",
            ("jellyfin", "a" * 40, "movie/1080p/Old Film", "torrent_removed"),
            None,
        )
        capture.emit(cleanup)
        assert not capture.warnings

        raw = "ERROR output=jellyfin hash=" + "b" * 40 + " title='Broken Film (2025)' operation=sync"
        error = logging.LogRecord(
            "torr2strm", logging.ERROR, __file__, 2, raw, (), None,
        )
        try:
            raise PermissionError("permission denied while writing STRM")
        except PermissionError:
            import sys
            error.exc_info = sys.exc_info()
        capture.emit(error)
        assert capture.warnings == []
        assert capture.errors == [raw]
        assert "PermissionError: permission denied while writing STRM" in capture.exception_details[raw]
        body = torr2strm_module._render_notification_body(capture.errors, "errors", capture)
        assert "Broken Film (2025)" in body
        assert "Не удалось синхронизировать медиа" in body
        assert "permission denied while writing STRM" in body
        assert "ERROR output=jellyfin" not in body


def test_torrserver_title_is_preserved_when_action_get_returns_not_found():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        cfg_data = cfg(base / "jelly", base / "kodi")
        h = "c" * 40

        class ListedButUnavailable:
            base_url = "http://127.0.0.1:8092"
            def list_torrents(self):
                return [{"Hash": h, "Title": "Definitely, Maybe (2008)", "Category": "movie", "Data": ""}]
            def get_torrent(self, torrent_hash):
                from torr2strm import TorrServerNotFound
                raise TorrServerNotFound(torrent_hash)

        capture = NotificationCapture()
        logger = logging.getLogger("torr2strm")
        logger.addHandler(capture)
        try:
            coordinator = SyncCoordinator(cfg_data, ListedButUnavailable(), dry_run=True, notification_capture=capture)
            snapshots, deferred = coordinator._get_snapshot_list()
        finally:
            logger.removeHandler(capture)

        assert snapshots == []
        assert deferred == {h}
        assert capture.torrent_titles[h] == "Definitely, Maybe (2008)"
        assert any("reason_code=not_found" in event for event in capture.warnings)
        body = torr2strm_module._render_notification_body(capture.warnings, "warnings", capture)
        assert "Definitely, Maybe (2008)" in body
        assert "HTTP 404" in body
        assert h not in body


def test_unresolved_collision_notification_includes_both_paths_and_hides_duplicate_sync_error():
    capture = NotificationCapture()
    h = "d" * 40
    capture.register_title(h, "Lexx (1997) Season 01 DVD collection")
    capture.errors.extend([
        f"STRM_COLLISION_UNRESOLVED output=jellyfin title='Lexx (1997) Season 01 DVD collection' hash={h} destination=tv/1080p/Lexx/Season 01/VTS_01_0.VOB.strm source=DVD2/VIDEO_TS/VTS_01_0.VOB",
        f"ERROR output=jellyfin hash={h} title='Lexx (1997) Season 01 DVD collection' operation=sync",
    ])
    raw_sync = capture.errors[1]
    capture.exception_details[raw_sync] = "RuntimeError: unresolved STRM destination collision"
    body = torr2strm_module._render_notification_body(capture.errors, "errors", capture)
    assert "Lexx (1997) Season 01 DVD collection" in body
    assert "Конфликт имён STRM не удалось разрешить" in body
    assert "tv/1080p/Lexx/Season 01/VTS_01_0.VOB.strm" in body
    assert "DVD2/VIDEO_TS/VTS_01_0.VOB" in body
    assert "Не удалось синхронизировать медиа" not in body
    assert "STRM_COLLISION_UNRESOLVED" not in body