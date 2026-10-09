import json
import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from torr2strm import (
    JacRedMatch,
    MediaInfoResolver,
    OutputRunner,
    OutputSpec,
    SyncCoordinator,
    bounded_strm_leaf,
    bounded_kodi_leaf,
    category_from_jacred_result,
    extract_btih,
    media_quality,
    quality_label_from_probe,
    quality_label_from_text,
    quality_label_from_metadata,
    quality_root_from_label,
    parse_snapshot,
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


class FakeJacRed:
    enabled = True

    def __init__(self, matches=None):
        self.matches = matches or {}
        self.calls = []

    def find_match(self, torrent):
        self.calls.append(torrent.hash)
        return self.matches.get(torrent.hash)


def cfg(jelly_root, kodi_root, remove=False, jacred=True):
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
        "jacred": {
            "url": "https://jac.red" if jacred else "",
            "api_key": "",
            "indexer_id": 1,
            "limit": 100,
            "timeout_sec": 2,
            "retries": 0,
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


def match_for(h, payload=None, category=None, magnet=None):
    return JacRedMatch(
        result={"infoHash": h, "category": category or "", "magnetUrl": magnet or ""},
        ffprobe=payload,
        magnet=magnet,
        category=category,
        query="fake",
    )


def run(cfg_data, client, jac):
    return SyncCoordinator(cfg_data, client, jac, dry_run=False).run()


def test_extract_btih_accepts_magnet():
    h = "064de6b3010e8a7f8df1098b8bab407af0405f86"
    assert extract_btih(f"magnet:?xt=urn:btih:{h}&dn=test") == h


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


def test_category_mapping_movie_tv_anime():
    assert category_from_jacred_result({"categories": [2045]}) == "movie"
    assert category_from_jacred_result({"categories": [5000]}) == "tv"
    assert category_from_jacred_result({"categories": [5060]}) == "_uncategorized"


def test_jacred_exact_match_supplies_category_and_ffprobe_and_kodi_magnet_once():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr = base / "jelly"
        kr = base / "kodi"
        h = "1" * 40
        magnet = f"magnet:?xt=urn:btih:{h}&dn=Example.Movie"
        t = torrent(h=h, title="Example Movie 2026", category="", paths=["Example.Movie.mkv"], data={})
        payload = probe(1920, 1080)
        result = {"infoHash": h, "categories": [2000], "magnetUrl": magnet, "ffprobe": payload["streams"]}
        jac = FakeJacRed({h: JacRedMatch(result, payload, magnet, "movie", "fake")})
        client = FakeClient([t], probes={(h, 1): payload})
        c = cfg(jr, kr, jacred=True)
        assert run(c, client, jac) == 0
        assert jac.calls == [h]
        assert client.ffprobe_calls == []
        jelly = list((jr / "movie" / "1080p").rglob("*.strm"))
        kodi = list((kr / "movie" / "1080p").rglob("*.strm"))
        assert len(jelly) == 1
        assert len(kodi) == 1
        assert "plugin.video.elementum/play?uri=" in kodi[0].read_text()
        assert "%3A%3Fxt%3Durn%3Abtih%3A" in kodi[0].read_text()
        assert "&oindex=" not in kodi[0].read_text()


def test_kodi_tv_uses_zero_based_original_filestats_order():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr = base / "jelly"
        kr = base / "kodi"
        h = "2" * 40
        t = torrent(
            h=h,
            title="Widow's Bay S01 2026",
            category="tv",
            paths=["back.jpg", "cover.jpg", "Widow.s.Bay.S01E01.mkv", "Widow.s.Bay.S01E02.mkv"],
            lengths=[100, 200, 500, 500],
        )
        probes = {(h, 3): probe(3840, 2160), (h, 4): probe(3840, 2160)}
        client = FakeClient([t], probes=probes)
        jac = FakeJacRed({h: None})
        c = cfg(jr, kr, jacred=False)
        assert run(c, client, jac) == 0
        strms = sorted((kr / "tv" / "4K").rglob("*.strm"))
        assert len(strms) == 2
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
        client = FakeClient([t], probes={(h, 1): payload})
        jac = FakeJacRed({h: match_for(h, payload, "movie", f"magnet:?xt=urn:btih:{h}&dn=Film")})
        c = cfg(jr, kr, jacred=True)
        assert run(c, client, jac) == 0
        first_calls = list(jac.calls)
        client.ffprobe_calls.clear()
        jac.calls.clear()
        assert run(c, client, jac) == 0
        assert client.ffprobe_calls == []
        assert jac.calls == [h]
        # There are two output copies of the same NFO, but the media probe is shared/reused.


def test_jellyfin_reverse_delete_is_authoritative_and_kodi_is_skipped():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr = base / "jelly"
        kr = base / "kodi"
        h = "4" * 40
        t = torrent(h=h, title="Film", category="movie")
        payload = probe()
        client = FakeClient([t], probes={(h, 1): payload})
        jac = FakeJacRed({h: match_for(h, payload, "movie", f"magnet:?xt=urn:btih:{h}&dn=Film")})
        c = cfg(jr, kr, remove=True, jacred=True)
        assert run(c, client, jac) == 0
        jelly_strm = next((jr / "movie" / "1080p").rglob("*.strm"))
        jelly_strm.unlink()
        client.ffprobe_calls.clear()
        assert run(c, client, jac) == 0
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
        jac = FakeJacRed({h: match_for(h, payload, "movie", f"magnet:?xt=urn:btih:{h}&dn=Film")})
        c = cfg(jr, kr, remove=False, jacred=True)
        assert run(c, client, jac) == 0
        kodi_strm = next((kr / "movie" / "1080p").rglob("*.strm"))
        kodi_strm.unlink()
        assert run(c, client, jac) == 0
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
        client = FakeClient([t], probes={(h, 1): payload})
        jac = FakeJacRed({})
        c = cfg(jr, kr, jacred=False)
        assert run(c, client, jac) == 0
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
        assert "<height>1080</height>" in tvshow_text
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
        jac = FakeJacRed({})
        c = cfg(jr, kr, jacred=False)
        assert run(c, client, jac) == 0
        for root in (jr, kr):
            manifest = json.loads((root / ".torr2strm" / "manifest.json").read_text())
            assert manifest["version"] == 5
            assert manifest["output"] in {"jellyfin", "kodi"}
            assert manifest["root"] == str(root)


def test_release_title_quality_fallback_materializes_unknown_media():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr = base / "jelly"
        kr = base / "kodi"
        h = "8" * 40
        t = torrent(h=h, title="Film 2160p HDR", category="movie")
        client = FakeClient([t], probes={})
        jac = FakeJacRed({})
        c = cfg(jr, kr, jacred=False)
        assert run(c, client, jac) == 0
        assert list((jr / "movie" / "4K").rglob("*.strm"))
        kodi_strm = next((kr / "movie" / "4K").rglob("*.strm"))
        assert "2160p HDR" in kodi_strm.name
        assert "[88888888]" in kodi_strm.name
        assert client.ffprobe_calls == [(h, 1)]


def test_blank_category_from_jacred_tv_and_anime_stays_uncategorized():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr = base / "jelly"
        kr = base / "kodi"
        h_tv = "9" * 40
        h_anime = "a" * 40
        tv = torrent(h=h_tv, title="Example Series S01", category="", paths=["Example.Series.S01E01.mkv"])
        anime = torrent(h=h_anime, title="Example Anime S01", category="", paths=["Example.Anime.S01E01.mkv"])
        p = probe(1920, 1080)
        matches = {
            h_tv: match_for(h_tv, p, "tv", f"magnet:?xt=urn:btih:{h_tv}&dn=Example.Series"),
            h_anime: match_for(h_anime, p, "_uncategorized", f"magnet:?xt=urn:btih:{h_anime}&dn=Example.Anime"),
        }
        client = FakeClient([tv, anime], probes={(h_tv, 1): p, (h_anime, 1): p})
        jac = FakeJacRed(matches)
        c = cfg(jr, kr, jacred=True)
        assert run(c, client, jac) == 0
        assert list((jr / "tv" / "1080p").rglob("*.strm"))
        assert list((jr / "_uncategorized" / "1080p").rglob("*.strm"))
        assert not list((jr / "tv" / "1080p").rglob("Example.Anime*"))


def test_nfo_format_v3_and_combination_tmdb_url_are_generated():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr = base / "jelly"
        kr = base / "kodi"
        h = "c" * 40
        t = torrent(h=h, title="The Prestige 2006", category="movie", data={"tmdbId": 1124, "imdbId": "tt0482571"})
        payload = probe(3840, 2160, codec="hevc")
        client = FakeClient([t], probes={(h, 1): payload})
        jac = FakeJacRed({})
        c = cfg(jr, kr, jacred=False)
        assert run(c, client, jac) == 0
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
        client = FakeClient([t], probes={(h, 1): payload})
        jac = FakeJacRed({})
        c = cfg(jr, kr, jacred=False)
        assert run(c, client, jac) == 0
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
        assert run(c, client, jac) == 0
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
        client = FakeClient([t], probes={(h, 1): p})
        jac = FakeJacRed({})
        c = cfg(jr, kr, jacred=False)
        assert run(c, client, jac) == 0
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
        client = FakeClient([t1, t2], probes={(h1, 1): p720, (h2, 1): p1080})
        assert run(cfg(jr, kr, jacred=False), client, FakeJacRed({})) == 0
        strms = list((kr / "tv" / "1080p" / "Canonical Series (2024)").rglob("*.strm"))
        assert len(strms) == 2
        assert len({p.name for p in strms}) == 2
        assert any("720p [11111111]" in p.name for p in strms)
        assert any("1080p [22222222]" in p.name for p in strms)
        tvshow_nfo = kr / "tv" / "1080p" / "Canonical Series (2024)" / "tvshow.nfo"
        assert tvshow_nfo.is_file()
        assert "<width>1280</width>" in tvshow_nfo.read_text(encoding="utf-8")
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
        assert run(cfg(jr, kr, jacred=False), client, FakeJacRed({})) == 0
        strm = next((kr / "tv" / "1080p" / "Mystery.Release.S01.720p").rglob("*.strm"))
        assert "DiscA/Season01" in str(strm)
        assert "S01E" not in strm.name
        assert "720p [33333333]" in strm.name
        assert not list((kr / "tv" / "1080p").rglob("tvshow.nfo"))


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
        c = cfg(jr, kr, jacred=False)
        assert run(c, client, FakeJacRed({})) == 0
        first = next((kr / "tv" / "1080p" / "Shared Show (2020)").rglob("*44444444*.strm"))
        client.torrents.pop(h1)
        assert run(c, client, FakeJacRed({})) == 0
        assert not first.exists()
        remaining = list((kr / "tv" / "1080p" / "Shared Show (2020)").rglob("*.strm"))
        assert len(remaining) == 1
        assert "[55555555]" in remaining[0].name
        assert (kr / "tv" / "1080p" / "Shared Show (2020)" / "tvshow.nfo").is_file()


def test_structured_quality_precedes_release_title_when_ffprobe_unavailable():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h = "e" * 40
        t = torrent(h=h, title="Film.2160p.HDR", category="movie", data={"quality": "720p"})
        client = FakeClient([t], probes={})
        assert run(cfg(jr, kr, jacred=False), client, FakeJacRed({})) == 0
        kodi_strm = next((kr / "movie" / "1080p").rglob("*.strm"))
        assert "720p" in kodi_strm.name
        assert "2160p" not in kodi_strm.name
        assert "HDR" not in kodi_strm.name


def test_unknown_quality_uses_1080p_root_without_quality_suffix():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jr, kr = base / "jelly", base / "kodi"
        h = "f" * 40
        t = torrent(h=h, title="Unclassified Movie Release", category="movie", data={})
        client = FakeClient([t], probes={})
        assert run(cfg(jr, kr, jacred=False), client, FakeJacRed({})) == 0
        kodi_strm = next((kr / "movie" / "1080p").rglob("*.strm"))
        assert " — " not in kodi_strm.name
        assert "[ffffffff]" in kodi_strm.name


def test_per_file_nfo_details_are_not_replaced_with_primary_quality_stream():
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
        client = FakeClient([t], probes={(h, 1): primary, (h, 2): secondary})
        assert run(cfg(jr, kr, jacred=False), client, FakeJacRed({})) == 0
        kodi_files = {p.name: p for p in (kr / "tv" / "4K" / "Season Pack").rglob("*.nfo")}
        assert len(kodi_files) == 2
        second = next(path for name, path in kodi_files.items() if "S01E02" in name)
        text = second.read_text(encoding="utf-8")
        assert "<width>1280</width>" in text
        assert "<height>720</height>" in text
        assert_no_display_title_fields(text)


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
        assert run(cfg(jr, kr, jacred=False), client, FakeJacRed({})) == 0
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
        assert run(cfg(jr, kr, jacred=False), client, FakeJacRed({})) == 0
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
        assert run(cfg(jr, kr, jacred=False), client, FakeJacRed({})) == 0
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
        assert run(cfg(jr, kr, jacred=False), client, FakeJacRed({})) == 0
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
        assert run(cfg(jr, kr, jacred=False), client, FakeJacRed({})) == 0
        names = sorted(p.name for p in (kr / "tv" / "1080p" / "Collision Show" / "Season 01").glob("*.strm"))
        assert len(names) == 2
        assert any("[12345678a]" in name for name in names)
        assert any("[12345678b]" in name for name in names)
