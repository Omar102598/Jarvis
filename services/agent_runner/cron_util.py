"""Standard-cron triggers for APScheduler.

APScheduler 3.x's CronTrigger.from_crontab() does NOT read the weekday field
the way cron does: it numbers weekdays from Monday (0 = mon … 6 = sun), while
cron numbers them from Sunday (0/7 = sun, 1 = mon …). So "0 23 * * 0", meant
as Sunday evening, fired on Monday. Every schedule with a weekday ran one day
late (Apollo's Sunday week plan, Chronicle's Sunday review, the Monday grocery
run and health overview, and chat-scheduled check-ins like "Sundays at 10").

cron_trigger() rewrites the weekday field into day NAMES, which APScheduler
reads unambiguously, then builds the trigger. Use it instead of from_crontab.
"""

from __future__ import annotations

from apscheduler.triggers.cron import CronTrigger

# Cron's numbering: 0 and 7 are both Sunday.
_CRON_DOW = {0: "sun", 1: "mon", 2: "tue", 3: "wed", 4: "thu", 5: "fri", 6: "sat", 7: "sun"}
_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]


def _dow_number(tok: str) -> int:
    tok = tok.strip().lower()
    if tok.isdigit():
        n = int(tok)
        if n not in _CRON_DOW:
            raise ValueError(f"weekday {n} out of range 0-7")
        return n % 7
    if tok[:3] in _NAMES:
        return _NAMES.index(tok[:3])
    raise ValueError(f"bad weekday '{tok}'")


def cron_dow_to_names(field: str) -> str:
    """Cron weekday field -> comma list of day names ('1-5' -> 'mon,tue,wed,thu,fri').

    '*' (optionally with a step over the whole week) is returned as-is when it
    has no step, since every day is every day in both numberings.
    """
    field = field.strip()
    if field in ("*", "?"):
        return "*"
    days: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", "?"):
            lo, hi = 0, 6
        elif "-" in part:
            a, b = part.split("-", 1)
            lo = _dow_number(a)
            # "x-7" means "through Sunday": keep 7 so the range doesn't invert.
            hi = 7 if b.strip() == "7" else _dow_number(b)
            if hi < lo:
                raise ValueError(f"weekday range '{part}' runs backwards")
        else:
            lo = hi = _dow_number(part)
        days.update(d % 7 for d in range(lo, hi + 1, step))
    return ",".join(_NAMES[d] for d in sorted(days))


def cron_trigger(expr: str, timezone=None) -> CronTrigger:
    """A CronTrigger that reads `expr` with standard cron semantics."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"expected 5 cron fields, got {len(fields)}: '{expr}'")
    minute, hour, dom, month, dow = fields
    return CronTrigger(minute=minute, hour=hour, day=dom, month=month,
                       day_of_week=cron_dow_to_names(dow), timezone=timezone)
