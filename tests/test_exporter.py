import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import exporter  # noqa: E402
from spotipy.exceptions import SpotifyException  # noqa: E402


def art(i, name, genres):
    return {"id": i, "name": name, "genres": genres, "popularity": 50}


A = {"a1": art("a1", "Band A", ["post-punk", "indie"]), "a2": art("a2", "Band B", ["indie"]),
     "a3": art("a3", "Band C", []), "a4": art("a4", "Band D", ["shoegaze"])}


def track(i, artist_ids):
    return {"id": i, "name": f"T{i}", "artists": [{"id": a, "name": A[a]["name"]} for a in artist_ids],
            "album": {"name": "Alb", "release_date": "2020"}}


class FakeSP:
    """Minimaler Spotify-Ersatz mit 2-seitigem Paging."""

    def next(self, page):
        return page["_next"]

    def current_user_top_artists(self, limit, time_range):
        p2 = {"items": [A["a2"]], "next": None}
        return {"items": [A["a1"]], "next": "x", "_next": p2}

    def current_user_top_tracks(self, limit, time_range):
        return {"items": [track("t1", ["a1"])], "next": None}

    def current_user_followed_artists(self, limit, after):
        return {"artists": {"items": [A["a3"]], "cursors": {"after": None}}}

    def current_user_saved_tracks(self, limit):
        return {"items": [{"track": track("t1", ["a1"])}, {"track": track("t2", ["a1", "a2"])},
                          {"track": track("t4", ["a4"])}, {"track": None}],
                "next": None}

    def current_user(self):
        return {"id": "me"}

    def current_user_playlists(self, limit):
        return {"items": [{"id": "p1", "name": "Mine", "owner": {"id": "me", "display_name": "Me"}},
                          {"id": "p2", "name": "Theirs", "owner": {"id": "x"}}], "next": None}

    def playlist(self, pid):
        return {"items": {"items": [{"item": track("t3", ["a2"])}, {"item": None}], "next": None}}

    def artists(self, ids):  # Sammel-Endpunkt: bei Dev-Mode-Apps 403
        raise SpotifyException(403, -1, "Forbidden")

    def artist(self, aid):
        self.single_calls.append(aid)
        return A[aid]

    single_calls = []


def test_artists_none_name():
    t = {"artists": [{"id": "x", "name": None}, {"id": "y", "name": "Ok"}, None]}
    assert exporter._artists(t) == "?, Ok"


def test_full_run(tmp_path):
    spec = {
        "output_dir": str(tmp_path),
        "exports": {
            "top_artists": {"time_ranges": ["short_term", "long_term"]},
            "top_tracks": {},
            "genres": {"source": "spotify", "sources": ["top_artists", "liked_tracks", "followed_artists", "playlists"], "file": "g.csv"},
            "liked_tracks": {},
            "followed_artists": {},
            "playlists": {},
        },
    }
    written = exporter.run(spec, exporter.Fetcher(FakeSP()), tmp_path)
    assert len(written) == 7

    def rows(name):
        with open(tmp_path / name, encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    assert [r["artist"] for r in rows("top_artists_short_term.csv")] == ["Band A", "Band B"]  # Paging
    assert [r["track"] for r in rows("playlists.csv")] == ["Tt3"]  # nur eigene Playlist
    g = {r["genre"]: float(r["weight"]) for r in rows("g.csv")}
    # indie: top a1 + top a2 + liked(t1:a1, t2:a1, t2:a2) + playlist(a2) = 1+1+2+1+1 = 6; post-punk: 1+2 = 3
    # shoegaze: nur über Einzelabfrage (a4 steht weder in Top noch Followed) -> Fallback greift
    assert g == {"indie": 6.0, "post-punk": 3.0, "shoegaze": 1.0}
    assert FakeSP.single_calls == ["a4"]  # Top/Followed-Künstler werden nicht erneut abgefragt
    assert len(rows("liked_tracks.csv")) == 3  # None-Track übersprungen


class FakeLastFm:
    TAGS = {"band a": [["post-punk", 100], ["seen live", 90], ["band a", 80], ["1983", 70], ["indie", 30]],
            "band b": [["indie", 100], ["shoegaze", 50]]}
    calls = []

    def top_tags(self, artist):
        self.calls.append(artist)
        return self.TAGS.get(artist.casefold(), [])

    def save(self):
        pass


def test_pick_tags_filters_noise():
    tags = FakeLastFm.TAGS["band a"]
    got = exporter.pick_tags(tags, "Band A", 40, 5, exporter.DEFAULT_EXCLUDE_TAGS)
    assert got == [("post-punk", 100)]  # seen live, Künstlername, Jahr, zu niedriger Count rausgefiltert


def test_genres_via_lastfm(tmp_path):
    f = exporter.Fetcher(FakeSP())
    f.lastfm = FakeLastFm()
    headers, rows = exporter.export_genres(f, {"sources": ["top_artists"], "time_ranges": ["long_term"]})
    g = {r["genre"]: r["weight"] for r in rows}
    # Band A (Gewicht 1): post-punk 1.0 | Band B: indie 1.0, shoegaze 0.5
    assert g == {"post-punk": 1.0, "indie": 1.0, "shoegaze": 0.5}
    assert sorted(FakeLastFm.calls) == ["Band A", "Band B"]  # Spotify-Artist-Endpunkt wird nicht angefasst


def test_lastfm_http_cache_and_errors(tmp_path, monkeypatch):
    import requests

    calls = []

    class Resp:
        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    def fake_get(url, params, **kw):
        calls.append(params["artist"])
        if params["artist"] == "Nobody":
            return Resp({"error": 6, "message": "The artist you supplied could not be found"})
        return Resp({"toptags": {"tag": [{"name": "Post-Punk", "count": 100}, {"name": "Seen Live", "count": "50"}]}})

    monkeypatch.setattr(requests, "get", fake_get)
    lf = exporter.LastFm("key", tmp_path / "c.json", delay=0)
    assert lf.top_tags("Band A") == [["post-punk", 100], ["seen live", 50]]
    assert lf.top_tags("band a") == [["post-punk", 100], ["seen live", 50]]  # Cache, case-insensitiv
    assert lf.top_tags("Nobody") == []
    assert calls == ["Band A", "Nobody"]
    lf.save()
    lf2 = exporter.LastFm("key", tmp_path / "c.json", delay=0)  # Cache überlebt Neustart
    assert lf2.top_tags("Nobody") == [] and calls == ["Band A", "Nobody"]


def test_disk_cache_avoids_second_fetch(tmp_path):
    class Counting(FakeSP):
        n = 0

        def current_user_saved_tracks(self, limit):
            Counting.n += 1
            return super().current_user_saved_tracks(limit)

    cp = tmp_path / "c.json"
    first = exporter.Fetcher(Counting(), cache_path=cp, ttl=3600).liked_tracks()
    # zweiter Lauf: kein Client nötig (Factory würde knallen), Daten kommen von der Platte
    def boom():
        raise AssertionError("Spotify-Client sollte nicht gebaut werden")
    second = exporter.Fetcher(cache_path=cp, ttl=3600, sp_factory=boom).liked_tracks()
    assert second == first and Counting.n == 1
    # --refresh holt neu
    exporter.Fetcher(Counting(), cache_path=cp, ttl=3600, refresh=True).liked_tracks()
    assert Counting.n == 2
    # ttl=0 schaltet Cache aus
    exporter.Fetcher(Counting(), cache_path=cp, ttl=0).liked_tracks()
    assert Counting.n == 3
