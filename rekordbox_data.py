"""Read-only access to the rekordbox library: track rows, beatgrids, phrases, waveforms.

Nothing here writes to rekordbox. The database is read from a temporary copy so it
is safe to use while rekordbox is running.
"""
import logging
import shutil
import struct
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text

logging.disable(logging.WARNING)  # pyrekordbox warns about files missing from our snapshot
from pyrekordbox import Rekordbox6Database  # noqa: E402

RB_DIR = Path.home() / "Library/Pioneer/rekordbox"
ANLZ_ROOT = RB_DIR / "share"

# Cue labels this tool writes; used to tell its cues apart from the DJ's own.
TOOL_CUE_COMMENTS = ("IN", "BODY", "DROP", "OUT")
SYNTHETIC_PHRASE_BEATS = 32

# Phrase labels by rekordbox "mood" (1 high, 2 mid, 3 low) and phrase kind.
PHRASE_LABELS = {
    1: {1: "intro", 2: "up", 3: "down", 5: "chorus", 6: "outro"},
    2: {1: "intro", 2: "verse", 3: "verse", 4: "verse", 5: "verse", 6: "verse", 7: "verse",
        8: "bridge", 9: "chorus", 10: "outro"},
    3: {1: "intro", 2: "verse", 3: "verse", 4: "verse", 5: "verse", 6: "verse", 7: "verse",
        8: "bridge", 9: "chorus", 10: "outro"},
}


@dataclass
class Phrase:
    label: str
    beat: int  # 1-based index into Analysis.beat_times
    start: float  # seconds


@dataclass
class Analysis:
    beat_times: list = field(default_factory=list)  # seconds, one per beat
    bar_positions: list = field(default_factory=list)  # 1-4, one per beat
    phrases: list = field(default_factory=list)
    end_beat: int = 0
    loudness: float = 0.0  # mean waveform height, 0-1
    synthetic_phrases: bool = False  # True when rekordbox had no phrase analysis

    def time_of_beat(self, beat):
        """Seconds at a 1-based beat index, clamped to the grid."""
        if not self.beat_times:
            return 0.0
        return self.beat_times[max(0, min(beat, len(self.beat_times)) - 1)]


def query(sql):
    """Run a read-only query against a snapshot of the live rekordbox database."""
    last_error = None
    for _ in range(3):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("master.db", "master.db-wal", "master.db-shm"):
                if (RB_DIR / name).exists():
                    shutil.copy2(RB_DIR / name, tmp)
            try:
                db = Rekordbox6Database(path=str(Path(tmp) / "master.db"))
                rows = db.session.execute(text(sql)).fetchall()
                db.close()
                return rows
            except Exception as e:  # snapshot taken mid-write; try again
                last_error = e
                time.sleep(0.5)
    raise RuntimeError(f"could not read rekordbox database: {last_error}")


def source_of(folder_path):
    """Where a track comes from: spotify, soundcloud, tidal, ... or local."""
    if folder_path.startswith("/"):
        return "local"
    return folder_path.split(":", 1)[0]


def playlists():
    """Ordinary playlists (no folders or smart lists), in rekordbox's own order."""
    # Names are read as raw bytes: some contain invalid UTF-8 that breaks normal decoding.
    rows = query(
        "select p.ID, cast(p.Name as blob), p.Seq from djmdPlaylist p "
        "where p.Attribute = 0 and p.SmartList is null order by p.Seq"
    )
    return [{"id": r[0], "name": bytes(r[1] or b"").decode("utf-8", "replace")} for r in rows]


def playlist_summaries():
    """{playlist id: {source: (tracks, analysed)}} for every playlist, in one query."""
    rows = query(
        "select s.PlaylistID, c.FolderPath, "
        "(c.Analysed > 0 and c.BPM > 0 and coalesce(c.AnalysisDataPath, '') != '') "
        "from djmdSongPlaylist s join djmdContent c on c.ID = s.ContentID"
    )
    summary = {}
    for playlist_id, folder_path, analysed in rows:
        counts = summary.setdefault(playlist_id, {}).setdefault(source_of(folder_path or ""), [0, 0])
        counts[0] += 1
        counts[1] += 1 if analysed else 0
    return summary


def playlist_tracks(playlist_id):
    """Tracks of a rekordbox playlist in playlist order, with what rekordbox knows about each."""
    rows = query(
        "select c.ID, c.FolderPath, c.Title, a.Name, c.Analysed, c.BPM, c.Length, k.ScaleName, "
        "c.AnalysisDataPath, s.TrackNo, "
        "(select group_concat(coalesce(x.Comment, ''), '|') from djmdCue x where x.ContentID = c.ID) "
        "from djmdSongPlaylist s join djmdContent c on c.ID = s.ContentID "
        "left join djmdArtist a on a.ID = c.ArtistID left join djmdKey k on k.ID = c.KeyID "
        f"where s.PlaylistID = '{int(playlist_id)}' order by s.TrackNo"
    )
    tracks = []
    for r in rows:
        source = source_of(r[1] or "")
        cue_comments = r[10].split("|") if r[10] is not None else []
        tracks.append({
            "id": r[0],
            "content_id": r[0],
            "source": source,
            "spotify_id": r[1].split(":")[-1] if source == "spotify" else None,
            "title": r[2] or "",
            "artist": r[3] or "",
            "analysed": bool(r[4]) and (r[5] or 0) > 0 and bool(r[8]),
            "bpm": (r[5] or 0) / 100,
            "length": r[6] or 0,
            "key": r[7] or "",
            "anlz": r[8] or "",
            "track_no": r[9],
            "own_cues": sum(1 for c in cue_comments if c not in TOOL_CUE_COMMENTS),
            "tool_cues": sum(1 for c in cue_comments if c in TOOL_CUE_COMMENTS),
        })
    return tracks


def _tags(path):
    """Yield (fourcc, header_bytes, body_bytes) for each section of an ANLZ file."""
    data = path.read_bytes()
    pos = struct.unpack(">I", data[4:8])[0]
    while pos + 12 <= len(data):
        fourcc = data[pos:pos + 4].decode("ascii", "replace")
        len_header, len_tag = struct.unpack(">II", data[pos + 4:pos + 12])
        if len_tag <= 0:
            break
        yield fourcc, data[pos:pos + len_header], data[pos + len_header:pos + len_tag]
        pos += len_tag


def read_analysis(anlz_path):
    """Parse beatgrid, phrases and waveform loudness from a track's ANLZ files."""
    result = Analysis()
    dat = ANLZ_ROOT / anlz_path.lstrip("/")
    mood, raw_phrases = 1, []
    for path in (dat, dat.with_suffix(".EXT")):
        if not path.exists():
            continue
        for fourcc, header, body in _tags(path):
            if fourcc == "PQTZ":
                for i in range(struct.unpack(">I", header[20:24])[0]):
                    bar_pos, _tempo, ms = struct.unpack(">HHI", body[i * 8:i * 8 + 8])
                    result.bar_positions.append(bar_pos)
                    result.beat_times.append(ms / 1000)
            elif fourcc == "PSSI":
                count, mood = struct.unpack(">HH", header[16:20])
                result.end_beat = struct.unpack(">H", header[26:28])[0]
                for i in range(count):
                    _index, beat, kind = struct.unpack(">HHH", body[i * 24:i * 24 + 6])
                    raw_phrases.append((beat, kind))
            elif fourcc == "PWV3" and body:
                heights = [b & 0x1F for b in body]
                result.loudness = sum(heights) / len(heights) / 31
    labels = PHRASE_LABELS.get(mood, PHRASE_LABELS[1])
    result.phrases = [
        Phrase(labels.get(kind, "phrase"), beat, result.time_of_beat(beat))
        for beat, kind in raw_phrases
    ]
    if not result.end_beat:
        result.end_beat = len(result.beat_times)
    if not result.phrases and result.beat_times:
        # No phrase analysis for this track: fall back to 8-bar blocks from the first downbeat.
        first = next((i + 1 for i, pos in enumerate(result.bar_positions) if pos == 1), 1)
        result.phrases = [
            Phrase("phrase", beat, result.time_of_beat(beat))
            for beat in range(first, result.end_beat - 15, SYNTHETIC_PHRASE_BEATS)
        ]
        result.synthetic_phrases = True
    return result
