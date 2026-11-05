# Spotify-Stil-Export

Docker-Tool, das beim Start sich selbst aktualisiert, Daten aus deinem Spotify-Account liest und gemäß `spec.yaml` als CSV ablegt – Grundlage, um den eigenen Musikstil zu beschreiben (Top-Künstler, Genres, Liked Songs, Playlists).
Angelehnt an [Spotify-Jellyfin-Export](https://git.binary-kitchen.de/j0nas/Spotify-Jellyfin-Export), aber neu geschrieben (das Original exportiert keine Genres/Top-Listen und holt komplette Diskografien).

## Einmalig
1. [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) → App anlegen, Redirect URI `http://127.0.0.1:1337` eintragen.
2. `cp .env.example .env` und Client ID/Secret eintragen. Für Genres zusätzlich einen kostenlosen [Last.fm-API-Key](https://www.last.fm/api/account/create) als `LASTFM_API_KEY`.
3. `docker build -t spotify-style-export .`

## Start (bei Bedarf)
```bash
docker run --rm -it --env-file .env -v "$PWD/data:/data" spotify-style-export
```
- **Erster Lauf:** URL aus dem Terminal im Browser öffnen, zustimmen, die (nicht ladbare) Redirect-URL aus der Adressleiste ins Terminal einfügen. Token liegt danach in `data/.spotify-token` – weitere Läufe ohne Login (`-it` dann nicht mehr nötig).
- **Vorgabe:** `data/spec.yaml` (wird beim ersten Start aus `spec.example.yaml` angelegt). Ergebnisse unter `data/out/`.
- **Update beim Start:** `spotipy`/`pyyaml` werden aktualisiert (offline: letzter Stand). Tool-Code kommt per `git pull` aus `TOOL_REPO` (im Dockerfile auf dieses Repo gesetzt; überschreibbar mit `-e TOOL_REPO=…` / `-e TOOL_REF=main`, leer lassen = eingebauter Code). Abschalten: `-e SKIP_UPDATE=1`.

## Ausgabe (CSV)
`top_artists_<range>.csv`, `top_tracks_<range>.csv`, `genres.csv` (Genre, Gewicht, Anteil %, Beispiel-Künstler), `liked_tracks.csv`, `followed_artists.csv`, `playlists.csv` – Namen/Pfade/Quellen in der Spec änderbar.

## Hinweise
- Spotify-Apps im Dev-Modus: nur freigeschaltete Nutzer (dein eigener Account reicht).
- Genres kommen standardmäßig von Last.fm (Top-Tags des Künstlers, nutzergepflegt) und nicht von Spotify, dessen Artist-Daten für Dev-Mode-Apps stark eingeschränkt sind. Tags werden in `data/.lastfm-cache.json` gecacht, Folgeläufe sind schnell. `source: spotify|auto` in der Spec schaltet um.
- Tests: `pip install spotipy pyyaml pytest && pytest`
