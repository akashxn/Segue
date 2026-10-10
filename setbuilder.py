"""Building blocks for a DJ set: cue placement, transition scoring, ordering, transition notes.

Everything here works from the BPM, key, beatgrid and phrase analysis rekordbox
stored for each track. Nothing in this module reads or writes rekordbox itself.
"""
import math
import random
import re


ARC_WEIGHT = 1.0  # how strongly the order follows the energy arc vs. smooth transitions
BLEND_LIMIT_PCT = 8.0  # above this tempo gap, suggest a cut instead of a blend

# Energy arc shapes as (position 0-1, energy 0-1) points.
ARCS = {
    "peak": [(0.0, 0.3), (0.75, 1.0), (1.0, 0.6)],
    "ramp": [(0.0, 0.0), (1.0, 1.0)],
    "flat": [(0.0, 0.5), (1.0, 0.5)],
}


# ---------- cue points ----------

def first_downbeat(analysis, beat):
    """First beat at or after `beat` that is beat 1 of a bar (within one bar)."""
    for b in range(beat, min(beat + 4, len(analysis.bar_positions) + 1)):
        if analysis.bar_positions[b - 1] == 1:
            return b
    return beat


def default_out(analysis, body, end):
    """Mix out at the start of the closing outro, if it leaves room to blend."""
    phrases = analysis.phrases
    trailing = len(phrases)
    while trailing > 0 and phrases[trailing - 1].label == "outro":
        trailing -= 1
    if trailing < len(phrases) and end - phrases[trailing].beat >= 16:
        return phrases[trailing].beat
    earlier = [p.beat for p in phrases if 16 <= end - p.beat <= 64 and p.beat > body]
    return earlier[0] if earlier else max(body, end - 32)


def place_cues(analysis, in_beat=None, out_beat=None):
    """Return cue beats: in, body (mixed in by here), drop (first chorus), out, end.

    in_beat/out_beat override where the track is entered and left (phrase starts).
    """
    phrases = analysis.phrases
    end = min(analysis.end_beat, len(analysis.beat_times))
    cue_in = first_downbeat(analysis, in_beat or (phrases[0].beat if phrases else 1))
    limit = out_beat or end

    body = next(
        (p.beat for p in phrases
         if 16 <= p.beat - cue_in and p.beat < limit and (in_beat or p.label != "intro")),
        min(cue_in + 32, limit),
    )
    drop = next((p.beat for p in phrases if p.label == "chorus" and p.beat >= body), None)
    out = out_beat or default_out(analysis, body, end)
    if drop is not None and not body < drop < out:
        drop = None
    return {"in": cue_in, "body": body, "drop": drop, "out": out, "end": end}


# ---------- transition scoring ----------

CLASSIC_TO_CAMELOT = {
    "Abm": "1A", "G#m": "1A", "Ebm": "2A", "D#m": "2A", "Bbm": "3A", "A#m": "3A", "Fm": "4A",
    "Cm": "5A", "Gm": "6A", "Dm": "7A", "Am": "8A", "Em": "9A", "Bm": "10A", "F#m": "11A",
    "Gbm": "11A", "Dbm": "12A", "C#m": "12A", "B": "1B", "F#": "2B", "Gb": "2B", "Db": "3B",
    "C#": "3B", "Ab": "4B", "G#": "4B", "Eb": "5B", "D#": "5B", "Bb": "6B", "A#": "6B", "F": "7B",
    "C": "8B", "G": "9B", "D": "10B", "A": "11B", "E": "12B",
}


def camelot(key):
    """(number, letter) for a key written as Camelot ("8A") or classic ("Am"), else None."""
    key = CLASSIC_TO_CAMELOT.get(key or "", key or "")
    m = re.fullmatch(r"(\d{1,2})([AB])", key)
    return (int(m.group(1)), m.group(2)) if m else None


def key_relation(a, b):
    """(description, cost 0-1) for mixing key a into key b."""
    ka, kb = camelot(a), camelot(b)
    if not ka or not kb:
        return "key unknown", 0.5
    step = (kb[0] - ka[0]) % 12
    step = step - 12 if step > 6 else step
    same_letter = ka[1] == kb[1]
    if step == 0:
        return ("same key", 0.0) if same_letter else ("relative major/minor", 0.15)
    if abs(step) == 1:
        return ("adjacent key", 0.1) if same_letter else ("diagonal key", 0.45)
    if abs(step) == 2 and same_letter:
        return "two steps apart", 0.5
    return "key clash", 1.0


def tempo_change(a, b):
    """Signed % change from a to b, treating half/double time as the same tempo."""
    if not a or not b:
        return 0.0
    return min(((b * f / a - 1) * 100 for f in (0.5, 1, 2)), key=abs)


def transition_cost(a, b):
    _, key_cost = key_relation(a["key"], b["key"])
    tempo_cost = min((abs(tempo_change(a["bpm"], b["bpm"])) / 6) ** 2, 4.0)
    return key_cost + tempo_cost


# ---------- ordering ----------

def ranks(values):
    """Map values to 0-1 by rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    for rank, i in enumerate(order):
        out[i] = rank / max(len(values) - 1, 1)
    return out


def arc_targets(n, shape):
    points = ARCS[shape]
    raw = []
    for i in range(n):
        x = i / max(n - 1, 1)
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            if x0 <= x <= x1:
                raw.append(y0 + (y1 - y0) * (x - x0) / (x1 - x0))
                break
    # A flat arc has no ranking to follow; leave every slot at mid energy.
    return ranks(raw) if len(set(raw)) > 1 else [0.5] * n


def order_set(tracks, shape, arc_weight=ARC_WEIGHT):
    """Order tracks to follow the energy arc while keeping transitions smooth."""
    n = len(tracks)
    if n < 3:
        return tracks
    targets = arc_targets(n, shape)
    pair = [[transition_cost(a, b) for b in tracks] for a in tracks]

    def cost(order):
        total = sum(pair[order[i]][order[i + 1]] for i in range(n - 1))
        return total + arc_weight * sum((tracks[t]["energy"] - targets[i]) ** 2 for i, t in enumerate(order))

    # Start with a perfect arc, then anneal towards smoother transitions.
    by_energy = sorted(range(n), key=lambda t: tracks[t]["energy"])
    order = [0] * n
    for slot, t in zip(sorted(range(n), key=lambda i: targets[i]), by_energy):
        order[slot] = t
    rng = random.Random(0)
    current = best = cost(order)
    best_order = order[:]
    steps = 60000
    for step in range(steps):
        temperature = 1.5 * (1 - step / steps) + 0.01
        i, j = sorted(rng.sample(range(n), 2))
        trial = order[:]
        if rng.random() < 0.5:
            trial[i], trial[j] = trial[j], trial[i]
        else:
            trial[i:j + 1] = reversed(trial[i:j + 1])
        candidate = cost(trial)
        if candidate <= current or rng.random() < math.exp((current - candidate) / temperature):
            order, current = trial, candidate
            if current < best:
                best, best_order = current, order[:]
    return [tracks[t] for t in best_order]


# ---------- output ----------

def mmss(seconds):
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def describe_transition(a, b):
    relation, key_cost = key_relation(a["key"], b["key"])
    change = tempo_change(a["bpm"], b["bpm"])
    overlap_beats = min(b["cues"]["body"] - b["cues"]["in"], a["cues"]["end"] - a["cues"]["out"], 64)
    bars = max(overlap_beats // 4, 1)
    if abs(change) > BLEND_LIMIT_PCT:
        rating = "hard"
        how = (f"Tempo jump too big to blend. At OUT ({mmss(a['cue_times']['out'])}) cut or echo out, "
               f"and start the next track from IN on the phrase.")
    else:
        rating = "smooth" if key_cost <= 0.15 and abs(change) <= 3 else "ok"
        how = (f"At OUT ({mmss(a['cue_times']['out'])}) start the next track from IN. Blend about "
               f"{bars} bars and be fully across by its BODY cue ({mmss(b['cue_times']['body'])}).")
    if relation == "key clash" and rating != "hard":
        rating = "clash"
        how += " Keys clash, so keep the overlap short."
    if a.get("pick", {}).get("transition"):
        how = a["pick"]["transition"]
    return {"rating": rating, "key": relation, "tempo_pct": round(change, 1), "bars": bars, "how": how}


def play_seconds(t):
    return t["cue_times"]["out"] - t["cue_times"]["in"]
