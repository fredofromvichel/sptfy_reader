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
            "genres": {"sources": ["top_artists", "liked_tracks", "followed_artists", "playlists"], "file": "g.csv"},
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
