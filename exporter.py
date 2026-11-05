#!/usr/bin/env python3
"""Spotify-Stil-Export: liest Daten aus Spotify und legt sie gemäß spec.yaml als CSV ab."""
import argparse
import csv
import json
import logging
import os
import re
import shutil
import sys
import time
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
    """Holt Daten einmalig und merkt sie sich (im Speicher und optional auf der Platte).

    Disk-Cache: Rohdaten pro Abruf mit Zeitstempel; gültig `ttl` Sekunden, `refresh` ignoriert ihn."""

    def __init__(self, sp=None, cache_path=None, ttl=0, refresh=False, sp_factory=None):
        self._sp, self._sp_factory = sp, sp_factory  # Client erst bei Bedarf bauen -> kein Login bei vollem Cache
        self.cache_path = Path(cache_path) if cache_path else None
        self.ttl, self.refresh = ttl, refresh
        self._cache = {}
        self._disk = {}
        if self.cache_path and ttl > 0:
            try:
                self._disk = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        self._artist_cache = {}
        self._batch_ok = True
        self._single_fail = 0
        self._logged_single_err = False

    @property
    def sp(self):
        if self._sp is None:
            self._sp = self._sp_factory()
        return self._sp

    def _memo(self, key, fn):
        """Ergebnis aus Speicher, sonst aus Disk-Cache (frisch genug), sonst per fn() holen."""
        if key in self._cache:
            return self._cache[key]
        entry = self._disk.get(key)
        if entry and not self.refresh and time.time() - entry["ts"] < self.ttl:
            age = int((time.time() - entry["ts"]) / 60)
            log.info("Cache: %s (%d min alt)", key, age)
            data = entry["data"]
        else:
            data = fn()
            if self.cache_path and self.ttl > 0:
                self._disk[key] = {"ts": time.time(), "data": data}
                try:
                    self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = self.cache_path.with_suffix(".tmp")
                    tmp.write_text(json.dumps(self._disk, ensure_ascii=False), encoding="utf-8")
                    tmp.replace(self.cache_path)
                except OSError as exc:
                    log.warning("Spotify-Cache nicht schreibbar: %s", exc)
        self._cache[key] = data
        return data

    def _paged(self, page):
        """Iteriert über alle Seiten eines Spotify-Paging-Objekts."""
        while page:
            yield from page["items"]
            page = self.sp.next(page) if page.get("next") else None

    def top_artists(self, time_range, limit=50):
        data = self._memo(f"top_artists:{time_range}:{limit}", lambda: list(self._paged(
            self.sp.current_user_top_artists(limit=min(limit, 50), time_range=time_range)))[:limit])
        self.remember_artists(data)
        if data and not getattr(self, "_logged_fields", False):
            self._logged_fields = True
            n_gen = sum(1 for a in data if a.get("genres"))
            log.info("Top-Artist-Felder: %s | %d von %d mit Genres", sorted(data[0]), n_gen, len(data))
        return data

    def top_tracks(self, time_range, limit=50):
        return self._memo(f"top_tracks:{time_range}:{limit}", lambda: list(self._paged(
            self.sp.current_user_top_tracks(limit=min(limit, 50), time_range=time_range)))[:limit])

    def followed_artists(self):
        def fetch():
            out, after = [], None
            while True:
                res = self.sp.current_user_followed_artists(limit=50, after=after)["artists"]
                out += res["items"]
                after = res.get("cursors", {}).get("after")
                if not after or not res["items"]:
                    break
            return out
        data = self._memo("followed", fetch)
        self.remember_artists(data)
        return data

    def liked_tracks(self, limit=None):
        def fetch():
            out = []
            for rec in self._paged(self.sp.current_user_saved_tracks(limit=50)):
                if rec.get("track"):
                    out.append({**rec["track"], "_added_at": rec.get("added_at", "")})
                if limit and len(out) >= limit:
                    break
            return out
        return self._memo(f"liked:{limit}", fetch)

    def playlists(self, own_only=True):
        """Liefert [(playlist_meta, [tracks])]. Playlists ohne lesbare Items werden übersprungen."""
        return self._memo(f"playlists:{own_only}", lambda: self._fetch_playlists(own_only))

    def _fetch_playlists(self, own_only):
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
        return result

    def remember_artists(self, artists):
        """Künstler-Objekte mit Genres (Top/Followed) vormerken, spart Zusatzabfragen."""
        for a in artists:
            if a and a.get("id") and "genres" in a:
                self._artist_cache[a["id"]] = a

    def _artist_single(self, aid):
        from spotipy.exceptions import SpotifyException
        try:
            a = self.sp.artist(aid)
            self._single_fail = 0
            return a
        except SpotifyException as exc:
            if exc.http_status not in (403, 404, 429):
                raise
            self._single_fail += 1
            if not self._logged_single_err:
                self._logged_single_err = True
                log.warning("Einzelabfrage /artists/{id} fehlgeschlagen: HTTP %s %s", exc.http_status, exc.msg)
            return None

    def artists_by_id(self, ids, max_new=None):
        """Objekte (mit Genres) zu Artist-IDs. `ids` nach Wichtigkeit sortiert; max_new begrenzt Nachschlagungen.

        Spotify hat den Sammel-Endpunkt GET /artists für Dev-Mode-Apps abgeschaltet (403) -> Fallback auf
        Einzelabfragen."""
        from spotipy.exceptions import SpotifyException
        missing = [i for i in dict.fromkeys(ids) if i and i not in self._artist_cache]
        if max_new is not None and len(missing) > max_new:
            log.info("%d Künstler ohne Genre-Info, schlage nur die %d wichtigsten nach.", len(missing), max_new)
            missing = missing[:max_new]
        for n in range(0, len(missing), 50):
            chunk = missing[n:n + 50]
            res = None
            if self._batch_ok:
                try:
                    res = self.sp.artists(chunk)["artists"]
                except SpotifyException as exc:
                    if exc.http_status not in (403, 404):
                        raise
                    log.warning("Sammelabfrage /artists nicht verfügbar (%s) - nutze Einzelabfragen.", exc.http_status)
                    self._batch_ok = False
            if res is None:
                res = []
                for aid in chunk:
                    res.append(self._artist_single(aid))
                    if self._single_fail >= 10:
                        log.warning("Künstler-Abfragen werden abgelehnt - breche Nachschlagen ab (Genres nur aus Top/Followed).")
                        self.remember_artists(res)
                        return {i: self._artist_cache[i] for i in ids if i in self._artist_cache}
            self.remember_artists(res)
        return {i: self._artist_cache[i] for i in ids if i in self._artist_cache}


# ----------------------------------------------------------------- Last.fm
# Beliebte, aber stilistisch wertlose Tags
DEFAULT_EXCLUDE_TAGS = {
    "seen live", "favorites", "favorite", "favourites", "favourite", "love", "awesome", "amazing", "good",
    "beautiful", "great", "best", "all", "my music", "albums i own", "owned", "spotify", "under 2000 listeners",
    "male vocalists", "female vocalists", "male vocalist", "female vocalist", "british", "american", "german",
    "usa", "uk", "swedish", "canadian", "french", "australian",
}


class LastFm:
    """artist.getTopTags mit Datei-Cache (Name -> [[tag, count], ...])."""

    URL = "https://ws.audioscrobbler.com/2.0/"

    def __init__(self, api_key, cache_path: Path, delay=0.25):
        self.api_key, self.cache_path, self.delay = api_key, Path(cache_path), delay
        try:
            self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.cache = {}
        self._dirty = 0

    def save(self):
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
            self._dirty = 0
        except OSError as exc:
            log.warning("Last.fm-Cache nicht schreibbar: %s", exc)

    def top_tags(self, artist):
        key = artist.casefold()
        if key in self.cache:
            return self.cache[key]
        import requests
        tags = None
        for attempt in range(4):
            try:
                r = requests.get(self.URL, timeout=15, headers={"User-Agent": "sptfy_reader/1.0"}, params={
                    "method": "artist.gettoptags", "artist": artist, "autocorrect": 1,
                    "api_key": self.api_key, "format": "json"})
                data = r.json()
            except (requests.RequestException, ValueError) as exc:
                log.warning("Last.fm-Fehler bei '%s': %s", artist, exc)
                time.sleep(2 ** attempt)
                continue
            err = data.get("error")
            if err in (10, 26):  # ungültiger/gesperrter Key -> sinnlos weiterzumachen
                self.save()
                sys.exit(f"Last.fm: {data.get('message')} (LASTFM_API_KEY prüfen)")
            if err in (8, 11, 16, 29):  # temporär / Rate-Limit
                time.sleep(2 ** attempt)
                continue
            if err:  # z. B. 6 = Künstler unbekannt -> leer cachen
                tags = []
            else:
                tags = [[t["name"].strip().lower(), int(t.get("count", 0))]
                        for t in (data.get("toptags", {}).get("tag") or [])[:15]]
            break
        if tags is None:
            return []  # nicht cachen, nächster Lauf versucht es erneut
        self.cache[key] = tags
        self._dirty += 1
        if self._dirty >= 25:
            self.save()
        time.sleep(self.delay)
        return tags


def pick_tags(tags, artist, min_count, max_tags, exclude):
    """Filtert Roh-Tags: Mindest-Count, Blocklist, Künstlername, reine Jahreszahlen."""
    out = []
    for name, count in tags:
        if count < min_count or name in exclude or name == artist.casefold() or re.fullmatch(r"\d{4}", name):
            continue
        out.append((name, count))
        if len(out) >= max_tags:
            break
    return out


# ----------------------------------------------------------------- Exporte
def _artists(track):
    return ", ".join(a.get("name") or "?" for a in track.get("artists") or [] if a)


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
         "release_date": t.get("album", {}).get("release_date", ""), "added_at": t.get("_added_at", ""),
         "url": (t.get("external_urls") or {}).get("spotify", "")}
        for t in f.liked_tracks(opt.get("limit"))  # ohne limit: alle
    ]
    return ["track", "artists", "album", "release_date", "added_at", "url"], rows


def export_playlists(f, opt):
    rows = [
        {"playlist": pl["name"], "owner": pl["owner"].get("display_name") or pl["owner"]["id"],
         "track": t["name"], "artists": _artists(t), "album": t.get("album", {}).get("name", "")}
        for pl, tracks in f.playlists(opt.get("own_only", True)) for t in tracks
    ]
    return ["playlist", "owner", "track", "artists", "album"], rows


def export_genres(f, opt):
    """Genre-Häufigkeiten. Jede Quelle gibt Künstlern Gewicht; Genres/Tags stammen vom Künstler.

    source: lastfm (Standard) | spotify | auto (Spotify-Genres, sonst Last.fm)."""
    source = opt.get("source", "lastfm")
    if source not in ("lastfm", "spotify", "auto"):
        sys.exit(f"genres: unbekannte source '{source}' (lastfm | spotify | auto)")
    sources = opt.get("sources", ["top_artists", "liked_tracks", "followed_artists"])
    weight, names = Counter(), {}  # artist_id -> Gewicht / Name

    def add(a, w=1):
        if a and a.get("id"):
            weight[a["id"]] += w
            names.setdefault(a["id"], a.get("name") or "")

    for src in sources:
        if src == "top_artists":
            for tr in opt.get("time_ranges", ["long_term"]):
                for a in f.top_artists(tr, 50):
                    add(a)
        elif src == "followed_artists":
            for a in f.followed_artists():
                add(a)
        elif src == "liked_tracks":
            for t in f.liked_tracks(opt.get("liked_limit")):
                for a in t.get("artists") or []:
                    add(a)
        elif src == "playlists":
            for _, tracks in f.playlists(opt.get("own_only", True)):
                for t in tracks:
                    for a in t.get("artists") or []:
                        add(a)
        else:
            sys.exit(f"genres: unbekannte Quelle '{src}'")

    ranked = [aid for aid, _ in weight.most_common()]
    limit = opt.get("max_artist_lookups", 300)
    if len(ranked) > limit:
        log.info("%d Künstler gefunden, werte die %d wichtigsten aus (max_artist_lookups).", len(ranked), limit)
        ranked = ranked[:limit]

    artist_genres = {}  # artist_id -> [(genre, anteil 0..1)]
    if source in ("spotify", "auto"):
        info = f.artists_by_id(ranked, limit)
        for aid in ranked:
            g = (info.get(aid) or {}).get("genres") or []
            if g:
                artist_genres[aid] = [(x, 1.0) for x in g]
        log.info("Spotify-Genres vorhanden für %d von %d Künstlern", len(artist_genres), len(ranked))
    if source in ("lastfm", "auto"):
        todo = [aid for aid in ranked if aid not in artist_genres and names.get(aid)]
        lf = getattr(f, "lastfm", None)
        if todo and lf is None:
            sys.exit("Für Genres via Last.fm fehlt LASTFM_API_KEY (kostenlos: https://www.last.fm/api/account/create).")
        if todo:
            exclude = DEFAULT_EXCLUDE_TAGS | {t.lower() for t in opt.get("exclude_tags", [])}
            min_count, max_tags = opt.get("min_tag_count", 40), opt.get("max_tags_per_artist", 5)
            log.info("Last.fm: lade Tags für %d Künstler (gecachte sind sofort da) ...", len(todo))
            for n, aid in enumerate(todo, 1):
                picked = pick_tags(lf.top_tags(names[aid]), names[aid], min_count, max_tags, exclude)
                if picked:
                    artist_genres[aid] = [(g, c / 100) for g, c in picked]
                if n % 50 == 0:
                    log.info("    %d/%d", n, len(todo))
            lf.save()
        log.info("Genre-Info vorhanden für %d von %d Künstlern", len(artist_genres), len(ranked))

    genre_w, genre_artists = Counter(), defaultdict(Counter)
    for aid, gl in artist_genres.items():
        for g, share in gl:
            genre_w[g] += weight[aid] * share
            genre_artists[g][names.get(aid) or aid] += weight[aid]
    total = sum(genre_w.values()) or 1
    rows = [
        {"genre": g, "weight": round(w, 2), "share_pct": round(100 * w / total, 2),
         "top_artists": "; ".join(n for n, _ in genre_artists[g].most_common(5))}
        for g, w in genre_w.most_common(opt.get("top_n", 50))
    ]
    if not rows:
        log.warning("Keine Genres gefunden (min_tag_count zu hoch oder Quelle liefert nichts?).")
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
    ap.add_argument("--refresh", action="store_true", default=os.getenv("REFRESH") == "1",
                    help="Spotify-Cache ignorieren und alles neu abrufen (oder REFRESH=1)")
    args = ap.parse_args()

    spec_path = Path(args.spec)
    if not spec_path.exists():
        example = Path(__file__).with_name("spec.example.yaml")
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(example, spec_path)
        log.info("Keine Vorgabe gefunden - Beispiel nach %s kopiert und verwendet.", spec_path)
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}

    ttl = float(spec.get("cache_hours", 24)) * 3600  # 0 = Cache aus
    fetcher = Fetcher(cache_path=data_dir / ".spotify-cache.json", ttl=ttl, refresh=args.refresh,
                      sp_factory=lambda: make_client(data_dir))
    if os.getenv("LASTFM_API_KEY"):
        fetcher.lastfm = LastFm(os.environ["LASTFM_API_KEY"], data_dir / ".lastfm-cache.json")
    written = run(spec, fetcher, data_dir / "out")
    print("\nFertig:")
    for p, n in written:
        print(f"  {p}  ({n} Zeilen)")


if __name__ == "__main__":
    main()
