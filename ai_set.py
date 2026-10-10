"""Ask Claude to order the set and pick where to enter and leave each track.

Runs the local `claude` command (Claude Code) in the background, so it uses your
Claude login and plan. Claude cannot hear the audio: it works from rekordbox's
phrase map plus what it knows about the songs. Its picks are saved to disk so the
set stays the same until you ask for a fresh pass.
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

SEARCH_PATH = os.pathsep.join([
    str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
    os.environ.get("PATH", ""),
])
MIN_PLAY_BEATS = 32
TIMEOUT_SECONDS = 1800
DEFAULT_MODEL = "sonnet"
EFFORT = "medium"

SCHEMA = {
    "type": "object",
    "properties": {
        "set_notes": {"type": "string"},
        "tracks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "in": {"type": "integer"},
                    "out": {"type": "integer"},
                    "why": {"type": "string"},
                    "transition": {"type": "string"},
                },
                "required": ["n", "in", "out", "why", "transition"],
            },
        },
    },
    "required": ["set_notes", "tracks"],
}

BRIEF = """You are an experienced open-format club DJ planning a set from the playlist below.

Decide two things:
1. The order of the set. Use each track at most once. You do not have to use them all:
   leave out any track that does not fit the flow (wrong mood, tempo or key with no good
   place for it) by simply not listing it. Shape the energy over the whole
   set (warm up, build, peak, a breather or two, strong finish), and keep neighbours
   mixable: compatible Camelot keys where you can, tempos within about 6% for blends.
   Bigger tempo or key jumps are fine when you plan a cut, echo-out or drop-in instead
   of a blend, and say so.
2. For each track, where to bring it in and where to start the next track. A real set
   does not play songs in full. Typically play the strongest 1 to 2.5 minutes: come in
   on the intro or just before the hook, and get out at a phrase boundary where the
   energy dips (after a chorus, into a verse, bridge or breakdown). Vary it: give
   anthems and peak moments longer, use quick cuts to keep momentum.

Each track lists its phrases as `index:label@time`. Pick `in` and `out` as phrase
indexes from that list; `out` must be later than `in`. `out` is the moment the next
track starts playing. The labels come from rekordbox's automatic analysis and are
sometimes wrong, so lean on your own knowledge of how each song is structured.

Return the tracks in set order. For each: `n` (the track number given below), `in`,
`out`, `why` (one short sentence on the in/out choice) and `transition` (one or two
short sentences telling the DJ how to mix out of this track into the next; for the last
track say how to end). `set_notes` is two or three sentences on the shape of the set.

Playlist ({count} tracks):
{tracks}
"""


def _mmss(seconds):
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def _track_line(n, t):
    phrases = " ".join(f'{i}:{p["label"]}@{_mmss(p["start"])}' for i, p in enumerate(t["phrases"]))
    return (f'{n}. "{t["title"]}" by {t["artist"]} | {t["bpm"]:.1f} BPM | key {t["key"]} | '
            f'length {_mmss(t["duration"])} | loudness {t["loudness"]:.2f}\n   {phrases}')


def claude_command():
    """Full path to the claude command. Apps opened from Finder get a bare PATH."""
    found = shutil.which("claude", path=SEARCH_PATH)
    if not found:
        raise RuntimeError("could not find the `claude` command (Claude Code). Is it installed?")
    return found


def ask_claude(tracks, model=None):
    """Return Claude's raw answer for these tracks."""
    prompt = BRIEF.format(
        count=len(tracks),
        tracks="\n".join(_track_line(n, t) for n, t in enumerate(tracks, 1)),
    )
    command = [
        claude_command(), "-p", "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
        "--tools", "", "--strict-mcp-config", "--model", model or DEFAULT_MODEL, "--effort", EFFORT,
    ]
    # A clean environment and empty folder, so the request is the same wherever this is
    # launched from and picks up nothing from this project or a parent Claude session.
    env = {k: os.environ[k] for k in ("HOME", "USER") if k in os.environ}
    env["PATH"] = SEARCH_PATH
    with tempfile.TemporaryDirectory() as empty_dir:
        try:
            result = subprocess.run(
                command, input=prompt, capture_output=True, text=True,
                cwd=empty_dir, env=env, timeout=TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"claude did not answer within {TIMEOUT_SECONDS // 60} minutes")
    if result.returncode != 0:
        raise RuntimeError(f"claude failed: {(result.stderr or result.stdout).strip()[:300]}")
    reply = json.loads(result.stdout)
    if reply.get("is_error"):
        raise RuntimeError(f"claude returned an error: {str(reply.get('result'))[:300]}")
    answer = reply.get("structured_output")
    if not answer:
        text = reply.get("result", "")
        answer = json.loads(text[text.index("{"):text.rindex("}") + 1])
    return answer


def claude_picks(tracks, picks_file, fresh=False, model=None):
    """Claude's answer for exactly this set of tracks, from `picks_file` unless `fresh`."""
    ids = sorted(t["id"] for t in tracks)
    if not fresh and picks_file.exists():
        saved = json.loads(picks_file.read_text())
        if saved.get("ids") == ids:
            return saved["answer"]
    answer = ask_claude(tracks, model)
    picks_file.parent.mkdir(parents=True, exist_ok=True)
    picks_file.write_text(json.dumps({"ids": ids, "answer": answer}, indent=1))
    return answer


def apply_picks(tracks, answer):
    """Turn Claude's answer into (ordered tracks, problems).

    Each returned track gets "pick" = {"in_beat", "out_beat", "why", "transition"} when
    Claude's choice is usable. Anything invalid is dropped and reported. Tracks Claude
    left out of the set are appended at the end, marked "left_out".
    """
    ordered, problems, seen = [], [], set()
    for item in answer.get("tracks", []):
        n = item.get("n")
        if not isinstance(n, int) or not 1 <= n <= len(tracks) or n in seen:
            problems.append(f"ignored an entry with track number {n!r}")
            continue
        seen.add(n)
        track = tracks[n - 1]
        phrases = track["phrases"]
        i, o = item.get("in"), item.get("out")
        valid = (
            isinstance(i, int) and isinstance(o, int) and 0 <= i < o < len(phrases)
            and phrases[o]["beat"] - phrases[i]["beat"] >= MIN_PLAY_BEATS
        )
        track["pick"] = {"why": item.get("why", ""), "transition": item.get("transition", "")}
        if valid:
            track["pick"].update(in_beat=phrases[i]["beat"], out_beat=phrases[o]["beat"])
        else:
            problems.append(f'"{track["title"]}": in/out {i!r}/{o!r} not usable, kept the default cues')
        ordered.append(track)
    for n, track in enumerate(tracks, 1):
        if n not in seen:
            track["left_out"] = True  # Claude found no place for it; it goes last, without cues
            ordered.append(track)
    return ordered, problems
