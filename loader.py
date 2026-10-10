"""Load a playlist's un-analysed tracks to rekordbox's decks so rekordbox analyses them.

rekordbox only analyses a Spotify track when it is loaded to a deck. This walks down
the playlist in order: a track that is already analysed is skipped, and each of the
others is loaded to whichever deck is free ("Load track to Deck 1 / 2", shift + left
or right) while the loader waits for the rekordbox database to show it as analysed.

Every load starts from a known place: click the first row, arrow up to the top of the
list, arrow down to the track. The list scrolls as the selection moves, so once the
loader is past the rows that fit on screen a click alone no longer lands on the first
track, and counting down from it loads the wrong one.

The playlist must be open in rekordbox and sorted by "#". rekordbox shows the list one
of two ways, every track or only the un-analysed ones. The loader assumes the first and
switches when a load analyses the track the other layout has on that row. Loads that
neither layout explains stop the run rather than press more keys.
"""
import subprocess
import time

import rekordbox_data as rb

APP_PROCESS = "rekordbox"
KEY_LEFT, KEY_RIGHT, KEY_DOWN, KEY_UP, KEY_SHIFT = 123, 124, 125, 126, 56
LOAD_KEYS = {1: KEY_LEFT, 2: KEY_RIGHT}  # with shift held, rekordbox's default mapping
DECKS = (1, 2)  # decks to load to; (1,) uses one deck only
KEY_GAP_SECONDS = 0.03
CLICK_GAP_SECONDS = 0.8  # clicks closer together than this make a double-click, which loads the row
LOAD_TIMEOUT_SECONDS = 45
LIST_REFRESH_SECONDS = 2
MAX_ATTEMPTS = 2  # loads of one track that may time out before giving up on it
MAX_MISSES = 6
NOT_IN_FRONT = "rekordbox is no longer the front window"
JXA_MOUSE = 'ObjC.import("CoreGraphics"); '

_last_click = 0.0


class Stopped(Exception):
    pass


def _osascript(script, javascript=False):
    command = ["osascript"] + (["-l", "JavaScript"] if javascript else []) + ["-e", script]
    return subprocess.run(command, capture_output=True, text=True)


def permission_problem():
    """Return a message if macOS will not let us send keys, else None. Sends a bare shift press."""
    result = _osascript(f'tell application "System Events" to key code {KEY_SHIFT}')
    if result.returncode != 0:
        return ("macOS is blocking keyboard control. Allow this app under System Settings > "
                "Privacy & Security > Accessibility (and Automation > System Events), then try again. "
                f"Details: {result.stderr.strip()[:200]}")
    return None


def frontmost_app():
    return _osascript(
        'tell application "System Events" to get name of first process whose frontmost is true'
    ).stdout.strip()


def mouse_position():
    out = _osascript(
        JXA_MOUSE + 'var p=$.CGEventGetLocation($.CGEventCreate(null)); p.x+","+p.y', javascript=True
    ).stdout.strip()
    x, y = out.split(",")
    return float(x), float(y)


def _require_rekordbox_in_front():
    if frontmost_app() != APP_PROCESS:
        raise Stopped(NOT_IN_FRONT)


def press_keys(steps):
    """Send a run of key presses to rekordbox. steps: (key code, shift held, how many times).

    One osascript call for the whole run; starting a process per key made a long
    list slow. Each press still checks that rekordbox is in front.
    """
    lines = []
    for key_code, shift, times in steps:
        if times > 0:
            lines += [
                f"repeat {times} times",
                f'if (name of first process whose frontmost is true) is not "{APP_PROCESS}" then error "{NOT_IN_FRONT}"',
                f"key code {key_code}" + (" using {shift down}" if shift else ""),
                f"delay {KEY_GAP_SECONDS}",
                "end repeat",
            ]
    if not lines:
        return
    result = _osascript('tell application "System Events"\n' + "\n".join(lines) + "\nend tell")
    if result.returncode != 0:
        if NOT_IN_FRONT in result.stderr:
            raise Stopped(NOT_IN_FRONT)
        raise RuntimeError(f"could not send keystrokes: {result.stderr.strip()}")


def click(x, y):
    global _last_click
    time.sleep(max(0.0, _last_click + CLICK_GAP_SECONDS - time.time()))
    _require_rekordbox_in_front()
    _osascript(
        JXA_MOUSE + f"var p=$.CGPointMake({x},{y});"
        "var d=$.CGEventCreateMouseEvent(null,$.kCGEventLeftMouseDown,p,$.kCGMouseButtonLeft);"
        "var u=$.CGEventCreateMouseEvent(null,$.kCGEventLeftMouseUp,p,$.kCGMouseButtonLeft);"
        "$.CGEventPost($.kCGHIDEventTap,d); delay(0.08); $.CGEventPost($.kCGHIDEventTap,u);",
        javascript=True,
    )
    _last_click = time.time()


def run(playlist_id, first_row, titles=None, log=print, should_stop=lambda: False,
        timeout=LOAD_TIMEOUT_SECONDS):
    """Analyse every un-analysed track in the playlist. Returns (analysed, still_pending)."""
    tracks = rb.playlist_tracks(playlist_id)
    order = [t["id"] for t in tracks]
    titles = titles or {t["id"]: t["title"] for t in tracks}
    pending = {t["id"] for t in tracks if not t["analysed"]}
    total = len(pending)
    if not pending:
        return 0, 0

    def name(track):
        return titles.get(track) or track

    log(f"{total} to analyse, {len(order) - total} already analysed and skipped.")
    x, y = first_row
    _osascript(f'tell application "{APP_PROCESS}" to activate')
    time.sleep(1.5)

    hides_analysed = False  # the list layout: every track, or only the un-analysed ones
    decks = list(DECKS)
    loading = {}  # deck -> the load in flight on it
    attempts = {}  # track -> loads of it that timed out
    retry_later = set()  # timed out on this pass down the list; tried again on the next
    timed_out = set()  # every track whose load timed out; rekordbox may still finish one late
    confirmed = False  # a load has analysed the track it was sent for
    layout_sure = False  # ...on a row where the two layouts have different tracks
    last_row = len(order)  # how far down the list may have scrolled since the last click
    settle_until = 0.0
    done = misses = surprises = 0
    shared = {deck: {"done": 0, "timed_out": 0} for deck in decks}  # loads made while the other deck was busy
    started = time.time()

    while True:
        if should_stop():
            raise Stopped("stopped")
        loaded_now = False

        # What has rekordbox finished since the last look?
        analysed = {t["id"] for t in rb.playlist_tracks(playlist_id) if t["analysed"]}
        for track in [t for t in order if t in pending and t in analysed]:
            pending.discard(track)
            retry_later.discard(track)
            done += 1
            log(f"[{done}/{total}] analysed {name(track)}")
            if hides_analysed:
                settle_until = time.time() + LIST_REFRESH_SECONDS  # its row is about to go
            deck = next((d for d, load in loading.items() if load["target"] == track), None)
            if deck is not None:
                load = loading.pop(deck)
                shared[deck]["done"] += load["shared"]
                confirmed, misses, surprises = True, 0, 0
                layout_sure = layout_sure or load["other"] is not None
                continue
            if track in timed_out or not loading:
                continue  # an earlier load that finished after its time ran out
            # Not a track we sent. Is it what the other layout has on a row we loaded?
            deck = next((d for d, load in loading.items() if load["other"] == track), None)
            if deck is not None:
                hides_analysed, layout_sure = not loading.pop(deck)["assumed"], False
                log("  rekordbox is listing " + ("only the un-analysed tracks" if hides_analysed
                                                  else "every track") + "; adjusting.")
            else:
                oldest = min(loading, key=lambda d: loading[d]["at"])
                log(f'  sent "{name(loading.pop(oldest)["target"])}" but rekordbox analysed "{name(track)}".')
                surprises += 1
            confirmed, last_row = False, len(order)
        if surprises >= 2:
            log("Stopping: rekordbox keeps loading a different track from the one sent. Check the "
                "playlist is sorted by # with nothing typed in its search box, then start again.")
            break

        # Loads that produced nothing.
        for deck, load in list(loading.items()):
            if time.time() - load["at"] <= timeout:
                continue
            del loading[deck]
            misses += 1
            confirmed, last_row = False, len(order)
            if load["other_was_analysed"] and not layout_sure:
                # The other layout has an analysed track on that row, which would explain it.
                hides_analysed = not load["assumed"]
            track = load["target"]
            attempts[track] = attempts.get(track, 0) + 1
            retry_later.add(track)
            timed_out.add(track)
            if attempts[track] >= MAX_ATTEMPTS:
                log(f"  could not analyse: {name(track)}")
            shared[deck]["timed_out"] += load["shared"]
            if len(decks) > 1 and shared[deck]["timed_out"] >= 2 and shared[deck]["timed_out"] > shared[deck]["done"]:
                decks = decks[:1]
                attempts.clear()  # those timeouts say nothing about the tracks themselves
                log("  Two decks at once is not working here; carrying on with deck 1 only.")
        if misses >= MAX_MISSES:
            log(f"Stopping: {MAX_MISSES} loads in a row produced no analysis.")
            break

        # Send the next track down the list to a free deck. Until one load has landed
        # where it was sent, only one is in flight at a time.
        free = [d for d in decks if d not in loading]
        if free and (confirmed or not loading) and time.time() >= settle_until:
            in_flight = {load["target"] for load in loading.values()}
            target = next((t for t in order if t in pending and t not in in_flight and t not in retry_later), None)
            if target is None and not loading:
                again = {t for t in retry_later if attempts.get(t, 0) < MAX_ATTEMPTS}
                if not again:
                    break
                retry_later -= again  # back to the top for the ones that timed out once
                continue
            if target is not None:
                unanalysed = [t for t in order if t in pending]
                visible, other_layout = (unanalysed, order) if hides_analysed else (order, unanalysed)
                row = visible.index(target)
                other = other_layout[min(row, len(other_layout) - 1)]
                click(x, y)
                press_keys([(KEY_UP, False, min(len(visible), last_row + 3)), (KEY_DOWN, False, row),
                            (LOAD_KEYS[free[0]], True, 1)])
                for load in loading.values():
                    load["shared"] = True
                loading[free[0]] = {"target": target, "at": time.time(), "assumed": hides_analysed,
                                    "other": other if other != target else None,
                                    "other_was_analysed": other not in pending, "shared": bool(loading)}
                last_row = row
                loaded_now = True
        if not loaded_now:
            time.sleep(1)

    if done:
        log(f"  about {(time.time() - started) / done:.0f} s per track")
    return done, len(pending)
