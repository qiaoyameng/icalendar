"""This package contains all functionality for timezones."""

from .tzid import is_utc, tzid_from_dt, tzid_from_tzinfo, tzids_from_tzinfo
from .tzp import _CURRENT_TZP, TZP

tzp = TZP()
"""The shared default timezone provider.

This is used whenever no explicit timezone parsing context is active.
See :class:`icalendar.timezone.tzp.TZP` for creating isolated contexts.
"""


def get_tzp() -> TZP:
    """Return the timezone provider of the active parsing context.

    If a :class:`TZP` context has been activated (for example by using
    it as a context manager or by passing it to
    :meth:`icalendar.Calendar.from_ical`), that context is returned.
    Otherwise the shared default :data:`tzp` is returned.
    """
    current = _CURRENT_TZP.get()
    return current if current is not None else tzp


def use_pytz():
    """Use pytz as the implementation that looks up and creates timezones."""
    tzp.use_pytz()


def use_zoneinfo():
    """Use zoneinfo as the implementation that looks up and creates timezones."""
    tzp.use_zoneinfo()


__all__ = [
    "TZP",
    "get_tzp",
    "is_utc",
    "tzid_from_dt",
    "tzid_from_tzinfo",
    "tzids_from_tzinfo",
    "tzp",
    "use_pytz",
    "use_zoneinfo",
]
