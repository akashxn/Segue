"""The pipeline, one rekordbox playlist at a time: status, analyse, build set, write back.

Each playlist keeps its working files in .cache/<playlist id>/ (Claude's saved
picks and the built set), so playlists never overwrite each other.
"""
import json
import subprocess
import time
from pathlib import Path

import ai_set
import loader
import rekordbox_data as rb
import setbuilder as sb
import spotify_data
import writecues

HERE = Path(__file__).parent
CACHE = HERE / ".cache"
ARRANGED_SUFFIX = " (arranged)"
BIG_PLAYLIST = 80  # above this, a Claude pass gets slow and may time out
REKORDBOX_APP = "/Applications/rekordbox 7/rekordbox.app"
KEEP_IN_SET = ("id", "content_id", "source", "title", "artist", "bpm", "key", "energy",
               "duration", "cues", "cue_times", "phrases", "own_cues", "pick", "left_out")


def playlist_dir(playlist_id):
    return CACHE / str(int(playlist_id))


def set_file(playlist_id):
    return playlist_dir(playlist_id) / "set.json"


def named_tracks(playlist_id):
    """Playlist tracks with readable titles (Spotify's are scrambled in rekordbox's database)."""
    tracks = rb.playlist_tracks(playlist_id)
    spotify_ids = [t["spotify_id"] for t in tracks if t["spotify_id"]]
    if spotify_ids:
        names = spotify_data.track_info(spotify_ids)
        for t in tracks:
            if t["spotify_id"]:
                t.update(names[t["spotify_id"]])
    return tracks


def track_list(playlist_id):
    """The playlist's tracks as the page lists them: names plus what rekordbox knows so far."""
    keep = ("id", "title", "artist", "source", "bpm", "key", "length", "analysed", "own_cues")
    return [{k: t[k] for k in keep} for t in named_tracks(playlist_id)]


def list_playlists():
    """Every playlist with what is needed to decide the next step for it."""
    summaries = rb.playlist_summaries()
    result = []
    for playlist in rb.playlists():
        by_source = summaries.get(playlist["id"], {})
        total = sum(n for n, _ in by_source.values())
        analysed = sum(done for _, done in by_source.values())
        spotify_total, spotify_done = by_source.get("spotify", (0, 0))
        result.append({
            **playlist,
            "tracks": total,
            "sources": {source: n for source, (n, _) in by_source.items()},
            "analysed": analysed,
            "to_analyse_spotify": spotify_total - spotify_done,
            "to_analyse_other": (total - analysed) - (spotify_total - spotify_done),
            "has_set": set_file(playlist["id"]).exists(),
            "is_arranged": playlist["name"].endswith(ARRANGED_SUFFIX),
        })
    return result


def playlist_name(playlist_id):
    return next(p["name"] for p in rb.playlists() if str(p["id"]) == str(playlist_id))


def analyse(playlist_id, first_row, log, should_stop):
    """Run the loader on the playlist's un-analysed Spotify tracks."""
    problem = loader.permission_problem()
    if problem:
        raise RuntimeError(problem)
    titles = {t["id"]: f'{t["title"]} - {t["artist"]}' for t in named_tracks(playlist_id)}
    done, left = loader.run(playlist_id, first_row, titles=titles, log=log, should_stop=should_stop)
    log(f"Finished: {done} analysed, {left} still to do.")


def build_set(playlist_id, use_ai=True, fresh=False, log=print):
    """Order the playlist and place cues. Saves and returns the set."""
    tracks, skipped = [], []
    for t in named_tracks(playlist_id):
        analysis = rb.read_analysis(t["anlz"]) if t["analysed"] else None
        if not analysis or not analysis.beat_times or not analysis.phrases:
            skipped.append(f'{t["title"]} - {t["artist"]}')
            continue
        t["analysis"] = analysis
        t["phrases"] = [{"label": p.label, "beat": p.beat, "start": p.start} for p in analysis.phrases]
        t["duration"] = t["length"] or analysis.beat_times[-1]
        t["loudness"] = analysis.loudness
        tracks.append(t)
    if len(tracks) < 2:
        raise RuntimeError("Fewer than two analysed tracks in this playlist; nothing to arrange.")

    # Energy: no audio to measure, so blend waveform loudness with tempo.
    loud, tempo = sb.ranks([t["loudness"] for t in tracks]), sb.ranks([t["bpm"] for t in tracks])
    for t, l, b in zip(tracks, loud, tempo):
        t["energy"] = 0.5 * l + 0.5 * b

    mode, notes, problems = "ordered by rules (energy arc, key and tempo)", "", []
    if use_ai:
        picks = playlist_dir(playlist_id) / "claude_picks.json"
        if fresh or not picks.exists():
            log(f"Asking Claude to plan {len(tracks)} tracks. This takes several minutes...")
        answer = ai_set.claude_picks(tracks, picks, fresh=fresh)
        ordered, problems = ai_set.apply_picks(tracks, answer)
        mode, notes = "ordered by Claude", answer.get("set_notes", "")
        for problem in problems:
            log(f"note: {problem}")
    else:
        ordered = sb.order_set(tracks, "peak")

    # Tracks Claude left out of the flow sit at the end of the order and get no cues.
    played = [t for t in ordered if not t.get("left_out")]
    for t in ordered:
        t["cues"] = t["cue_times"] = {}
    for t in played:
        pick = t.get("pick", {})
        t["cues"] = sb.place_cues(t["analysis"], pick.get("in_beat"), pick.get("out_beat"))
        t["cue_times"] = {k: t["analysis"].time_of_beat(v) if v else None for k, v in t["cues"].items()}
    transitions = [sb.describe_transition(a, b) for a, b in zip(played, played[1:])]

    result = {
        "playlist_id": str(playlist_id),
        "playlist_name": playlist_name(playlist_id),
        "built_at": time.strftime("%Y-%m-%d %H:%M"),
        "mode": mode,
        "notes": notes,
        "problems": problems,
        "skipped": skipped,
        "minutes": round(sum(sb.play_seconds(t) for t in played) / 60),
        "tracks": [{k: t[k] for k in KEEP_IN_SET if k in t} for t in ordered],
        "transitions": transitions,
    }
    set_file(playlist_id).parent.mkdir(parents=True, exist_ok=True)
    set_file(playlist_id).write_text(json.dumps(result, indent=1))
    log(f'Set built: {len(played)} tracks, about {result["minutes"]} minutes, '
        f'{len(ordered) - len(played)} left out by Claude, {len(skipped)} not analysed.')
    return result


def load_set(playlist_id):
    path = set_file(playlist_id)
    return json.loads(path.read_text()) if path.exists() else None


def arranged_name(playlist_id):
    name = playlist_name(playlist_id)
    return name if name.endswith(ARRANGED_SUFFIX) else name + ARRANGED_SUFFIX


def quit_rekordbox(log):
    if not writecues.rekordbox_running():
        return False
    log("Quitting rekordbox...")
    subprocess.run(["osascript", "-e", 'tell application "rekordbox" to quit'], capture_output=True)
    for _ in range(40):
        if not writecues.rekordbox_running():
            time.sleep(1)  # let it finish flushing its database
            return True
        time.sleep(1)
    raise RuntimeError("rekordbox did not quit. Close it yourself and try again.")


def add_to_rekordbox(playlist_id, log):
    """Quit rekordbox, back up, write cues and the arranged playlist, reopen rekordbox."""
    built = load_set(playlist_id)
    if not built:
        raise RuntimeError("Build the set first.")
    was_running = quit_rekordbox(log)
    try:
        writecues.write_set(built["tracks"], arranged_name(playlist_id), log=log)
    finally:
        if was_running:
            log("Reopening rekordbox (it may ask you to log in to Spotify again)...")
            subprocess.run(["open", "-a", REKORDBOX_APP], capture_output=True)
    log('Done. In rekordbox, open the "(arranged)" playlist and click the # column to sort it.')
