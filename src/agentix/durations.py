"""Ages given on the command line, such as 14d or 12h. They need no Modal."""

import datetime as dt
import re

from .errors import InvalidRequest

_UNITS = {"w": 7 * 86400, "d": 86400, "h": 3600, "m": 60, "s": 1}
_PART = re.compile(r"(\d{1,12})([wdhms])")
_WHOLE = re.compile(r"(?:\d{1,12}[wdhms])+")
# Modal keeps a snapshot for 30 days, so nothing recorded is much older. The
# cap keeps the date arithmetic in range.
MAXIMUM = dt.timedelta(days=3650)


def parse(text: object, flag: str = "--older-than") -> dt.timedelta:
    """A duration such as 14d, 12h, 30m, or 1d12h, as a timedelta.

    A bare number is refused: it is not clear whether it means days or
    seconds. So is zero, which would match everything.
    """
    value = str(text).strip().lower() if text is not None else ""
    if not _WHOLE.fullmatch(value):
        raise InvalidRequest(
            f"Invalid {flag}: {text!r}. Give a number with a unit: w (weeks), "
            "d (days), h (hours), m (minutes), or s (seconds), such as 14d, "
            "12h, 30m, or 1d12h."
        )
    seconds = sum(int(n) * _UNITS[unit] for n, unit in _PART.findall(value))
    if seconds <= 0:
        raise InvalidRequest(f"Invalid {flag}: {text!r}. It must be more than 0.")
    if seconds > MAXIMUM.total_seconds():
        raise InvalidRequest(
            f"Invalid {flag}: {text!r}. It can be at most {MAXIMUM.days}d."
        )
    return dt.timedelta(seconds=seconds)


def describe(age: dt.timedelta) -> str:
    """A short form of `age` for a listing, such as 16d 3h, 5h 20m, or 42s."""
    seconds = max(0, int(age.total_seconds()))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"
