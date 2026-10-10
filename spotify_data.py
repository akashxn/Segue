"""Track names and playlist contents from Spotify's public embed pages (no login or API key)."""
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CACHE_FILE = Path(__file__).parent / ".cache" / "tracks.json"


def _embed_entity(kind, spotify_id):
    req = urllib.request.Request(
        f"https://open.spotify.com/embed/{kind}/{spotify_id}",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    html = urllib.request.urlopen(req, timeout=30).read().decode()
    data = json.loads(re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S
    ).group(1))
    return data["props"]["pageProps"]["state"]["data"]["entity"]


def playlist_track_ids(url):
    """Track ids of a public Spotify playlist, in playlist order (first 100 only)."""
    playlist_id = re.search(r"playlist[/:]([A-Za-z0-9]+)", url).group(1)
    tracks = _embed_entity("playlist", playlist_id)["trackList"]
    return [t["uri"].split(":")[-1] for t in tracks if t.get("entityType") == "track"]


def _track_info(track_id):
    try:
        entity = _embed_entity("track", track_id)
        return {
            "title": entity.get("name") or entity.get("title") or track_id,
            "artist": ", ".join(a["name"] for a in entity.get("artists", [])),
        }
    except Exception:
        return None


def track_info(track_ids):
    """Return {track_id: {"title", "artist"}}. Looked up once, then cached on disk."""
    cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
    missing = [t for t in track_ids if t not in cache]
    if missing:
        with ThreadPoolExecutor(max_workers=6) as pool:
            for track_id, info in zip(missing, pool.map(_track_info, missing)):
                if info:
                    cache[track_id] = info
        CACHE_FILE.parent.mkdir(exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache, indent=1))
    return {t: cache.get(t, {"title": t, "artist": ""}) for t in track_ids}
