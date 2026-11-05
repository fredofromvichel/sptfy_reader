#!/usr/bin/env python3
"""Spotify-Stil-Export: liest Daten aus Spotify und legt sie gemäß spec.yaml als CSV ab."""
import argparse
import csv
import logging
import os
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

log = logging.getLogger("exporter")

SCOPES = "user-top-read user-library-read playlist-read-private user-follow-read"
DEFAULT_REDIRECT = "http://127.0.0.1:1337"
TIME_RANGES = ("short_term", "medium_term", "long_term")


# ----------------------------------------------------------------- Auth
def make_client(data_dir: Path):
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth

    cid, secret = os.getenv("SPOTIPY_CLIENT_ID"), os.getenv("SPOTIPY_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("SPOTIPY_CLIENT_ID / SPOTIPY_CLIENT_SECRET fehlen (siehe .env.example).")
    auth = SpotifyOAuth(
        client_id=cid,
        client_secret=secret,
        redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI", DEFAULT_REDIRECT),
        scope=SCOPES,
        cache_path=str(data_dir / ".spotify-token"),
        open_browser=False,
    )
    if not auth.get_cached_token():
        # Der lokale Redirect-Server von spotipy lauscht im Container nur auf Loopback und wäre
        # von außen nicht erreichbar -> manueller Ablauf: URL öffnen, Redirect-URL zurückkopieren.
        if not sys.stdin.isatty():
            sys.exit("Erster Login nötig: Container mit `docker run -it ...` starten.")
        print("\n1) Diese URL im Browser öffnen und zustimmen:\n\n   " + auth.get_authorize_url())
        print("\n2) Der Browser leitet auf eine nicht ladbare Seite (127.0.0.1) um -")
        print("   die komplette URL aus der Adressleiste hier einfügen.\n")
        code = auth.parse_response_code(input("Redirect-URL: ").strip())
        auth.get_access_token(code, as_dict=False, check_cache=False)
    return spotipy.Spotify(auth_manager=auth, requests_timeout=20, retries=5)


# ----------------------------------------------------------------- Daten holen
class Fetcher:
    """Holt Daten einmalig und merkt sie sich, damit mehrere Exporte sie wiederverwenden."""

    def __init__(self, sp):
        self.sp = sp
        self._cache = {}
        self._artist_cache = {}

    def _paged(self, page):
        """Iteriert über alle Seiten eines Spotify-Paging-Objekts."""
        while page:
            yield from page["items"]
            page = self.sp.next(page) if page.get("next") else None

    def top_artists(self, time_range, limit=50):
        k = ("top_artists", time_range, limit)
        if k not in self._cache:
            self._cache[k] = list(self._paged(self.sp.current_user_top_artists(limit=min(limit, 50), time_range=time_range)))[:limit]
        return self._cache[k]

    def top_tracks(self, time_range, limit=50):
        k = ("top_tracks", time_range, limit)
        if k not in self._cache:
            self._cache[k] = list(self._paged(self.sp.current_user_top_tracks(limit=min(limit, 50), time_range=time_range)))[:limit]
        return self._cache[k]

    def followed_artists(self):
        if "followed" not in self._cache:
            out, after = [], None
            while True:
                res = self.sp.current_user_followed_artists(limit=50, after=after)["artists"]
                out += res["items"]
                after = res.get("cursors", {}).get("after")
                if not after or not res["items"]:
                    break
            self._cache["followed"] = out
        return self._cache["followed"]

    def liked_tracks(self, limit=None):
        k = ("liked", limit)
        if k not in self._cache:
            out = []
            for rec in self._paged(self.sp.current_user_saved_tracks(limit=50)):
                if rec.get("track"):
                    out.append(rec["track"])
                if limit and len(out) >= limit:
                    break
            self._cache[k] = out
        return self._cache[k]

    def playlists(self, own_only=True):
        """Liefert [(playlist_meta, [tracks])]. Playlists ohne lesbare Items werden übersprungen."""
        k = ("playlists", own_only)
        if k in self._cache:
            return self._cache[k]
        me = self.sp.current_user()["id"]
        result = []
        for pl in self._paged(self.sp.current_user_playlists(limit=50)):
            if not pl:
                continue
            if own_only and pl["owner"]["id"] != me:
                continue
            try:
                full = self.sp.playlist(pl["id"])
            except Exception as exc:  # 403/404 bei fremden/kollaborativen Playlists
                log.warning("Playlist '%s' übersprungen: %s", pl.get("name"), exc)
                continue
            block = full.get("items") or full.get("tracks")  # API nennt das Feld je nach Version anders
            if not block:
                log.warning("Playlist '%s' ohne lesbare Items, übersprungen", pl.get("name"))
                continue
            tracks = []
            for rec in self._paged(block):
                t = rec.get("item") or rec.get("track")
                if t and t.get("id"):
                    tracks.append(t)
            result.append((pl, tracks))
        self._cache[k] = result
        return result

    def artists_by_id(self, ids):
        """Voll-Objekte (mit Genres) zu Artist-IDs, in 50er-Batches."""
        missing = [i for i in dict.fromkeys(ids) if i and i not in self._artist_cache]
        for n in range(0, len(missing), 50):
            for a in self.sp.artists(missing[n:n + 50])["artists"]:
                if a:
                    self._artist_cache[a["id"]] = a
        return {i: self._artist_cache[i] for i in ids if i in self._artist_cache}


# ----------------------------------------------------------------- Exporte
def _artists(track):
    return ", ".join(a["name"] for a in track.get("artists", []))


def export_top_artists(f, opt, time_range):
    rows = [
        {"rank": n, "artist": a["name"], "genres": "; ".join(a.get("genres", [])), "popularity": a.get("popularity", "")}
        for n, a in enumerate(f.top_artists(time_range, opt.get("limit", 50)), 1)
    ]
    return ["rank", "artist", "genres", "popularity"], rows


def export_top_tracks(f, opt, time_range):
    rows = [
        {"rank": n, "track": t["name"], "artists": _artists(t), "album": t.get("album", {}).get("name", "")}
        for n, t in enumerate(f.top_tracks(time_range, opt.get("limit", 50)), 1)
    ]
    return ["rank", "track", "artists", "album"], rows


def export_followed_artists(f, opt):
    rows = [{"artist": a["name"], "genres": "; ".join(a.get("genres", []))} for a in f.followed_artists()]
    return ["artist", "genres"], rows


def export_liked_tracks(f, opt):
    rows = [
        {"track": t["name"], "artists": _artists(t), "album": t.get("album", {}).get("name", ""),
         "release_date": t.get("album", {}).get("release_date", "")}
        for t in f.liked_tracks(opt.get("limit"))
    ]
    return ["track", "artists", "album", "release_date"], rows


def export_playlists(f, opt):
    rows = [
        {"playlist": pl["name"], "owner": pl["owner"].get("display_name") or pl["owner"]["id"],
         "track": t["name"], "artists": _artists(t), "album": t.get("album", {}).get("name", "")}
        for pl, tracks in f.playlists(opt.get("own_only", True)) for t in tracks
    ]
    return ["playlist", "owner", "track", "artists", "album"], rows


def export_genres(f, opt):
    """Genre-Häufigkeiten. Jede Quelle gibt Künstlern Gewicht; Genres stammen vom Künstler."""
    sources = opt.get("sources", ["top_artists", "liked_tracks", "followed_artists"])
    weight = Counter()  # artist_id -> Gewicht
    for src in sources:
        if src == "top_artists":
            for tr in opt.get("time_ranges", ["long_term"]):
                for a in f.top_artists(tr, 50):
                    weight[a["id"]] += 1
        elif src == "followed_artists":
            for a in f.followed_artists():
                weight[a["id"]] += 1
        elif src == "liked_tracks":
            for t in f.liked_tracks(opt.get("liked_limit")):
                for a in t.get("artists", []):
                    weight[a["id"]] += 1
        elif src == "playlists":
            for _, tracks in f.playlists(opt.get("own_only", True)):
                for t in tracks:
                    for a in t.get("artists", []):
                        weight[a["id"]] += 1
        else:
            sys.exit(f"genres: unbekannte Quelle '{src}'")

    info = f.artists_by_id(list(weight))
    genre_w, genre_artists = Counter(), defaultdict(Counter)
    for aid, w in weight.items():
        a = info.get(aid)
        if not a:
            continue
        for g in a.get("genres", []):
            genre_w[g] += w
            genre_artists[g][a["name"]] += w
    total = sum(genre_w.values()) or 1
    rows = [
        {"genre": g, "weight": w, "share_pct": round(100 * w / total, 2),
         "top_artists": "; ".join(n for n, _ in genre_artists[g].most_common(5))}
        for g, w in genre_w.most_common(opt.get("top_n", 50))
    ]
    if not rows:
        log.warning("Keine Genres gefunden (liefert die API für Künstler evtl. keine 'genres' mehr?).")
    return ["genre", "weight", "share_pct", "top_artists"], rows


# name -> (Funktion, ob pro time_range)
EXPORTS = {
    "top_artists": (export_top_artists, True),
    "top_tracks": (export_top_tracks, True),
    "followed_artists": (export_followed_artists, False),
    "liked_tracks": (export_liked_tracks, False),
    "playlists": (export_playlists, False),
    "genres": (export_genres, False),
}


def write_csv(path: Path, headers, rows, delimiter=","):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=headers, delimiter=delimiter)
        w.writeheader()
        w.writerows(rows)


def run(spec: dict, fetcher: Fetcher, base_out: Path):
    out_dir = Path(spec.get("output_dir") or base_out)
    if not out_dir.is_absolute():
        out_dir = base_out / out_dir
    delimiter = spec.get("delimiter", ",")
    written = []
    for name, opt in (spec.get("exports") or {}).items():
        opt = opt or {}
        if name not in EXPORTS:
            sys.exit(f"Unbekannter Export '{name}'. Möglich: {', '.join(EXPORTS)}")
        if not opt.get("enabled", True):
            continue
        fn, per_range = EXPORTS[name]
        template = opt.get("file", f"{name}_{{time_range}}.csv" if per_range else f"{name}.csv")
        ranges = opt.get("time_ranges", ["long_term"]) if per_range else [None]
        for tr in ranges:
            if tr is not None and tr not in TIME_RANGES:
                sys.exit(f"{name}: ungültiger time_range '{tr}' (erlaubt: {', '.join(TIME_RANGES)})")
            log.info("Export %s%s ...", name, f" ({tr})" if tr else "")
            headers, rows = fn(fetcher, opt, tr) if per_range else fn(fetcher, opt)
            path = out_dir / template.format(time_range=tr or "")
            write_csv(path, headers, rows, delimiter)
            written.append((path, len(rows)))
    return written


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data_dir = Path(os.getenv("DATA_DIR", "/data"))
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--spec", default=str(data_dir / "spec.yaml"))
    args = ap.parse_args()

    spec_path = Path(args.spec)
    if not spec_path.exists():
        example = Path(__file__).with_name("spec.example.yaml")
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(example, spec_path)
        log.info("Keine Vorgabe gefunden - Beispiel nach %s kopiert und verwendet.", spec_path)
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}

    fetcher = Fetcher(make_client(data_dir))
    written = run(spec, fetcher, data_dir / "out")
    print("\nFertig:")
    for p, n in written:
        print(f"  {p}  ({n} Zeilen)")


if __name__ == "__main__":
    main()
