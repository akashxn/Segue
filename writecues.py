"""Write a finished set into rekordbox: cue points on each track plus an ordered playlist.

Cue points:
  IN    start the track here
  BODY  be fully mixed in by here
  DROP  first chorus (when there is one)
  OUT   start the next track here

Tracks without cues get them as hot cues A-D. Tracks that already carry the DJ's
own cues keep those untouched and get memory cues instead. Cues this tool wrote
earlier (recognised by their labels) are replaced.

This changes the rekordbox database, so it only runs with rekordbox closed and
always saves a backup first.
"""
import json
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

import rekordbox_data as rb
from pyrekordbox import Rekordbox6Database
from pyrekordbox.db6 import tables

HERE = Path(__file__).parent
BACKUP_DIR = HERE / "backups"
DB_FILES = ("master.db", "master.db-wal", "master.db-shm", "masterPlaylists6.xml")
PLAYLIST_FILE = "masterPlaylists6.xml"

# cue name -> rekordbox hot cue Kind (pads A, B, C, D). Kind 4 is not a hot cue slot.
HOT_CUE_KINDS = {"in": 1, "body": 2, "drop": 3, "out": 5}
MEMORY_CUE_KIND = 0
FRAMES_PER_SECOND = 150


def rekordbox_running():
    return bool(subprocess.run(["pgrep", "-x", "rekordbox"], capture_output=True).stdout)


def backup(db_dir=rb.RB_DIR):
    target = BACKUP_DIR / datetime.now().strftime("master-%Y%m%d-%H%M%S")
    target.mkdir(parents=True, exist_ok=True)
    for name in DB_FILES:
        if (Path(db_dir) / name).exists():
            shutil.copy2(Path(db_dir) / name, target)
    return target


def commit(db):
    """Commit our changes only.

    pyrekordbox's own commit() also re-syncs the playlist file, which fails on
    playlist names it cannot decode. We update that file ourselves where needed.
    """
    db.registry.autoincrement_local_update_count(set_row_usn=True)
    db.session.commit()
    db.registry.clear_buffer()


def _json_time(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _cue_json(cue):
    """One cue in the form rekordbox keeps in contentCue.Cues."""
    item = {
        "ID": cue.ID, "ContentID": cue.ContentID, "ContentUUID": cue.ContentUUID,
        "InMsec": cue.InMsec, "InFrame": cue.InFrame, "InMpegFrame": cue.InMpegFrame,
        "InMpegAbs": cue.InMpegAbs, "OutMsec": cue.OutMsec, "OutFrame": cue.OutFrame,
        "OutMpegFrame": cue.OutMpegFrame, "OutMpegAbs": cue.OutMpegAbs,
        "Kind": cue.Kind, "Color": cue.Color,
    }
    if cue.Comment:
        item["Comment"] = cue.Comment
    item.update(UUID=cue.UUID, created_at=_json_time(cue.created_at), updated_at=_json_time(cue.updated_at))
    return item


def _sync_content_cue(db, content, now):
    """Rebuild the per-track cue summary row from the track's djmdCue rows."""
    db.flush()
    cues = db.get_cue(ContentID=content.ID).all()
    summary = db.get_content_cue(ContentID=content.ID).one_or_none()
    if not cues:
        if summary is not None:
            db.delete(summary)
        return
    if summary is None:
        summary = tables.ContentCue(
            ID=content.UUID, ContentID=content.ID, UUID=str(uuid.uuid4()), created_at=now,
        )
        db.add(summary)
    summary.Cues = json.dumps([_cue_json(c) for c in cues], separators=(",", ":"))
    summary.rb_cue_count = len(cues)
    summary.updated_at = now


def write_track_cues(db, track, now):
    """Replace this tool's cues on one track. Returns a short description of what was done."""
    content = db.get_content(ID=track["content_id"])
    if content is None:
        return "not in the rekordbox collection any more"
    existing = db.get_cue(ContentID=content.ID).all()
    own = [c for c in existing if c.Comment not in rb.TOOL_CUE_COMMENTS]
    for cue in existing:
        if cue.Comment in rb.TOOL_CUE_COMMENTS:
            db.delete(cue)
    db.flush()

    written = 0
    for name, hot_kind in HOT_CUE_KINDS.items():
        seconds = track["cue_times"].get(name)
        if seconds is None:
            continue
        msec = round(seconds * 1000)
        db.add(tables.DjmdCue(
            ID=str(db.generate_unused_id(tables.DjmdCue)), ContentID=content.ID,
            InMsec=msec, InFrame=msec * FRAMES_PER_SECOND // 1000, InMpegFrame=0, InMpegAbs=0,
            OutMsec=-1, OutFrame=0, OutMpegFrame=0, OutMpegAbs=0,
            Kind=MEMORY_CUE_KIND if own else hot_kind, Color=-1, Comment=name.upper(),
            ContentUUID=content.UUID, UUID=str(uuid.uuid4()), created_at=now, updated_at=now,
        ))
        written += 1
    _sync_content_cue(db, content, now)
    content.CueUpdated = str(int(content.CueUpdated or 0) + written)
    return f"{written} memory cues (kept your {len(own)} cues)" if own else f"{written} hot cues"


def upsert_playlist(db, name, tracks, now):
    """Create the playlist, or replace its contents if it exists. Returns its id."""
    # Plain SQL for lookups: loading playlist rows through the ORM fails on names
    # that are not valid UTF-8.
    found = db.session.execute(
        text("select ID from djmdPlaylist where Name = :name and ParentID = 'root'"), {"name": name}
    ).fetchone()
    if found:
        playlist_id = found[0]
        for song in db.get_playlist_songs(PlaylistID=playlist_id).all():
            db.delete(song)
        db.flush()
    else:
        playlist_id = str(db.generate_unused_id(tables.DjmdPlaylist))
        next_seq = db.session.execute(
            text("select coalesce(max(Seq), 0) + 1 from djmdPlaylist where ParentID = 'root'")
        ).scalar()
        db.add(tables.DjmdPlaylist(
            ID=playlist_id, Seq=next_seq, Name=name, Attribute=0, ParentID="root",
            UUID=str(uuid.uuid4()), created_at=now, updated_at=now,
        ))
    number = 0
    for track in tracks:
        if db.get_content(ID=track["content_id"]) is None:
            continue
        number += 1
        db.add(tables.DjmdSongPlaylist(
            ID=str(uuid.uuid4()), PlaylistID=playlist_id, ContentID=track["content_id"],
            TrackNo=number, UUID=str(uuid.uuid4()), created_at=now, updated_at=now,
        ))
    return playlist_id


def _register_in_playlist_file(db_dir, playlist_id, now):
    """rekordbox also lists every playlist in masterPlaylists6.xml; add ours there."""
    path = Path(db_dir) / PLAYLIST_FILE
    xml = path.read_text(encoding="utf-8")
    node_id = f"{int(playlist_id):X}"
    if f'Id="{node_id}"' in xml:
        return
    node = (f'    <NODE Id="{node_id}" ParentId="0" Attribute="0" '
            f'Timestamp="{int(now.timestamp() * 1000)}" Lib_Type="0" CheckType="0"/>\n')
    marker = "  </PLAYLISTS>"
    if marker not in xml:
        raise RuntimeError(f"unexpected layout in {PLAYLIST_FILE}")
    path.write_text(xml.replace(marker, node + marker), encoding="utf-8")


def write_set(tracks, playlist_name, db_dir=rb.RB_DIR, log=print):
    """Back up, then write cues for every track and the ordered playlist in one go."""
    if rekordbox_running():
        raise RuntimeError("rekordbox is running. Quit it first.")
    log(f"Backup saved to {backup(db_dir)}")
    db = Rekordbox6Database(path=str(Path(db_dir) / "master.db"))
    now = datetime.now(timezone.utc)
    try:
        for number, track in enumerate(tracks, 1):
            log(f'{number:>3}. {track["title"][:44]:<44} {write_track_cues(db, track, now)}')
        playlist_id = upsert_playlist(db, playlist_name, tracks, now)
        commit(db)
        _register_in_playlist_file(db_dir, playlist_id, now)
        log(f'Playlist "{playlist_name}" now holds {len(tracks)} tracks in set order.')
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
