from datetime import date, datetime
from typing import ClassVar

from icalendar.parser import Parameters


class TimeBase:
    """Make classes with a datetime/date comparable."""

    default_value: ClassVar[str]
    params: Parameters
    ignore_for_equality = {"TZID", "VALUE"}

    @staticmethod
    def _datetime_matches(first, second) -> bool:
        """Compare DATE-TIME values robustly around dateutil fold transitions."""
        if first == second:
            return True
        if not (isinstance(first, datetime) and isinstance(second, datetime)):
            return False
        if first.tzinfo is None or second.tzinfo is None:
            return False
        return (
            first.replace(tzinfo=None) == second.replace(tzinfo=None)
            and first.utcoffset() == second.utcoffset()
        )

    def __eq__(self, other):
        """self == other"""
        if isinstance(other, date):
            if isinstance(self.dt, tuple):
                return self.dt == other
            return self._datetime_matches(self.dt, other) or self.dt == other
        if isinstance(other, TimeBase):
            default = object()
            for key in (
                set(self.params) | set(other.params)
            ) - self.ignore_for_equality:
                if key[:2].lower() != "x-" and self.params.get(
                    key, default
                ) != other.params.get(key, default):
                    return False
            return self._datetime_matches(self.dt, other.dt) or self.dt == other.dt
        from .list import vDDDLists

        if isinstance(other, vDDDLists):
            return other == self
        return False

    def __hash__(self):
        return hash(self.dt)

    from icalendar.param import RANGE, RELATED, TZID

    def __repr__(self):
        """String representation."""
        return f"{self.__class__.__name__}({self.dt}, {self.params})"


__all__ = ["TimeBase"]
