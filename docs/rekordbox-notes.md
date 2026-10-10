# How rekordbox stores things

Notes from building Segue against rekordbox 7 on macOS. None of this is documented by
rekordbox and any of it can change in an update.

## Files

| File | Role |
|---|---|
| `app.py` | Local HTTP server on 127.0.0.1 + pywebview window. One background job at a time, polled by the page. |
| `ui.html` | The single page (vanilla JS, no build step). |
| `macwindow.py` | Mac window dressing: hidden title bar, traffic lights in the sidebar, app name. |
| `make_icon.py` | Draws the app icon with Quartz into `assets/`. |
| `core.py` | The pipeline per rekordbox playlist id: list/status, analyse, build set, add to rekordbox. |
| `rekordbox_data.py` | Read-only access: playlists, tracks, ANLZ parsing (beatgrid, phrases, waveform loudness). |
| `loader.py` | Drives rekordbox's UI to load Spotify tracks to a deck so rekordbox analyses them. |
| `setbuilder.py` | Cue placement, key/tempo transition scoring, rules-based ordering, transition notes. |
| `ai_set.py` | Calls `claude -p` for order + in/out picks, validates them against the phrase grid. |
| `spotify_data.py` | Track titles/artists from Spotify's public embed pages (no API key). |
| `writecues.py` | The only module that writes to rekordbox: cues, playlist, backup. |

Working files live in `.cache/<playlist id>/` (`set.json`, `claude_picks.json`),
`.cache/tracks.json` (Spotify title cache) and `.cache/ui.json` (the playlist and tab
last open). Backups go to `backups/`. Both folders are ignored by git.

## Library

- Library: `~/Library/Pioneer/rekordbox/master.db` (SQLCipher, opened via `pyrekordbox`).
  Analysis files: `~/Library/Pioneer/rekordbox/share/PIONEER/USBANLZ/...`.
- Always read from a copy (`rekordbox_data.query` copies db + wal + shm to a temp dir).
  rekordbox keeps the live db open in WAL mode.
- Streaming tracks have `FolderPath` like `spotify:track:<id>`, `soundcloud:tracks:<id>`,
  `tidal:tracks:<id>`. Spotify titles and artists are encrypted in the db (`$A7:v1:...`),
  so names come from `spotify_data`. Other sources are plain text.
- Some playlist names contain invalid UTF-8. Loading playlists through the pyrekordbox
  ORM, or calling its `db.commit()`, crashes on them. Use raw SQL with
  `cast(Name as blob)` for reads and `writecues.commit()` for writes.

## Analysis

- rekordbox only analyses a Spotify track when it is loaded to a deck. That is why
  `loader.py` exists. Analysis takes roughly 4-14 s per track.
- The track list comes in two layouts: every track stays listed, or tracks vanish once
  analysed and the keyboard selection is dropped. The loader assumes the first and
  switches when a load analyses the track the other layout has on that row.
- The list scrolls as the selection moves (16 rows fit at the usual window size), so a
  click at a fixed point only hits the first track while the list is at the top. Every
  load clicks, arrows up to the top, then arrows down to the track.
- Load shortcuts are shift + left (deck 1) and shift + right (deck 2), from the default
  key mapping. Plain letter keys drive the decks (Z is play), so the loader only ever
  sends arrows.
- Phrase data (PSSI tag, `.EXT` file) gives intro/verse/chorus/outro. `pyrekordbox`'s
  ANLZ parser fails on some files (PQT2 tag), so `rekordbox_data._tags` is its own
  parser. Tracks without phrases get synthetic 8-bar blocks.

## Cues and playlists

- Cues: `djmdCue` rows (`Kind` 1,2,3,5 = hot cue A,B,C,D; 0 = memory cue;
  `InFrame = InMsec * 150 // 1000`) plus one `contentCue` row per track holding all its
  cues as JSON, plus the `djmdContent.CueUpdated` counter. Hot cue E-H kinds (6-9) are
  seen in libraries but unverified for writing.
- Playlists: `djmdPlaylist` + `djmdSongPlaylist` (`TrackNo` order) and a `<NODE>` entry
  in `masterPlaylists6.xml` (Id = playlist id in uppercase hex).

## Rules for anything that writes

- rekordbox must be closed. `core.add_to_rekordbox` quits it, writes, and reopens it.
- Take a backup first (`writecues.backup`), every time.
- Test write changes on a copy of the library before touching a real one: copy
  `master.db*` and `masterPlaylists6.xml` to a scratch folder and pass `db_dir=`.
- Never modify or delete the DJ's own cues. Tool cues are recognised by their labels
  (`IN`, `BODY`, `DROP`, `OUT`) and only those are replaced. Tracks that already have
  the DJ's cues get the tool's as memory cues instead of hot cues.
- Never reorder or edit the original playlist; write to `<name> (arranged)`.
- Do not patch or modify the rekordbox application itself.

## The Claude call

`ai_set.ask_claude` runs `claude -p --output-format json --json-schema ... --tools ""
--strict-mcp-config --model sonnet --effort medium` in an empty temp dir with a clean
environment (only HOME, USER, PATH). The clean environment matters: launched from inside
another Claude session it otherwise inherits that session's settings and takes far longer.
Claude picks phrase indexes, and `apply_picks` rejects anything that is not a real phrase
boundary. Picks are cached per playlist so the set is stable until "Ask Claude Again".
