"""Pure helpers for editing Apollo's rolling lift split (no Redis, no I/O).

The split lives in user:plan:recomp["split"]["workouts"] as an ordered list of
{"id": "A", "focus": "Back and triceps"}; list order IS the rotation order.
Apollo (services/agent_runner/workout_agent.py::rotation_for_week) assigns a
lift day the workout at index

    (lift days scheduled from start_date up to that day + workout:rotation_offset)
        % len(workouts)

so moving "where we are" in the rotation is done purely through the offset,
the same knob skip_workout_day turns. lift_days_before() below must count lift
days exactly the way rotation_for_week does, or the two drift apart.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def lift_days(template: dict) -> set[str]:
    """Weekday names the week_template marks as lift days (mirrors Apollo)."""
    return {d for d, v in (template or {}).items() if str(v).strip().lower() == "lift"}


def lift_days_before(start: date, end: date, lift: set[str]) -> int:
    """Lift days in [start, end) — the `n` rotation_for_week computes."""
    n, d = 0, start
    while d < end:
        if d.strftime("%A") in lift:
            n += 1
        d += timedelta(days=1)
    return n


def next_lift_day(on_or_after: date, lift: set[str]) -> date | None:
    for i in range(7):
        d = on_or_after + timedelta(days=i)
        if d.strftime("%A") in lift:
            return d
    return None


def workout_index_on(day: date, start: date, lift: set[str], offset: int, count: int) -> int:
    """Rotation index Apollo gives the lift day `day` (day >= start)."""
    return (lift_days_before(start, day, lift) + offset) % count


def offset_for(day: date, start: date, lift: set[str], want_index: int, count: int) -> int:
    """Offset that makes lift day `day` land on rotation index `want_index`."""
    return (want_index - lift_days_before(start, day, lift)) % count


def parse_workouts(spec: str) -> dict[str, str]:
    """'A=back and triceps; B: chest, biceps' -> {'A': 'Back and triceps', 'B': 'Chest, biceps'}.

    Entries are separated by ';', ',' or newlines, and each starts with a single
    letter followed by '=' or ':'. A comma inside a focus is fine as long as
    it isn't followed by another 'X=' marker.
    """
    out: dict[str, str] = {}
    spec = (spec or "").strip()
    if not spec:
        return out
    parts = re.split(r"(?:^|[;,\n])\s*([A-Za-z])\s*[=:]\s*", spec)
    if parts[0].strip(" ;,\n"):
        raise ValueError(f"couldn't read '{parts[0].strip()}' — use the form 'A=back and triceps; B=...'")
    for letter, focus in zip(parts[1::2], parts[2::2]):
        focus = focus.strip(" ;,\n")
        if not focus:
            raise ValueError(f"workout {letter.upper()} has no muscle groups")
        out[letter.upper()] = focus[0].upper() + focus[1:]
    return out


def parse_order(spec: str) -> list[str]:
    """'A, C, B' / 'A -> C -> B' / 'ACB' -> ['A', 'C', 'B']."""
    letters = [c.upper() for c in re.findall(r"[A-Za-z]", spec or "")]
    if len(set(letters)) != len(letters):
        raise ValueError("each workout may appear only once in the rotation order")
    return letters


def apply_split_edit(workouts: list[dict], focus_updates: dict[str, str],
                     order: list[str]) -> list[dict]:
    """New ordered workout list. Focus updates edit existing letters or add new
    ones (appended); `order`, when given, is the complete rotation — letters
    left out are dropped."""
    by_id = {w["id"]: dict(w) for w in workouts}
    seq = [w["id"] for w in workouts]
    for letter, focus in focus_updates.items():
        if letter in by_id:
            by_id[letter]["focus"] = focus
        else:
            by_id[letter] = {"id": letter, "focus": focus}
            seq.append(letter)
    if order:
        unknown = [x for x in order if x not in by_id]
        if unknown:
            raise ValueError(f"workout(s) {', '.join(unknown)} aren't defined — give their "
                             "muscle groups too, e.g. 'D=arms'")
        seq = order
    if not seq:
        raise ValueError("the rotation can't be empty")
    return [by_id[x] for x in seq]


def describe(workouts: list[dict]) -> str:
    """'A = back and triceps, B = chest and biceps, C = legs and shoulders'."""
    return ", ".join(f"{w['id']} = {w['focus'].lower()}" for w in workouts)


def memory_fact(prefix: str, workouts: list[dict], template: dict,
                next_day: date | None, next_id: str | None) -> str:
    """The single remembered statement of the split, regenerated on every change
    so recall() never contradicts what Apollo actually programs."""
    order = " → ".join(w["id"] for w in workouts)
    other = [f"{d} {template[d]}" for d in WEEKDAYS
             if d in (template or {}) and str(template[d]).strip().lower() != "lift"]
    text = f"{prefix}: {describe(workouts)}; rotation order {order}."
    if other:
        text += f" Non-lifting days ({', '.join(other)}) stay as Apollo planned."
    if next_day and next_id:
        text += (f" Next lift day: {next_day.strftime('%A')} {next_day.isoformat()} "
                 f"does {next_id}, then the rotation keeps rolling.")
    return text
