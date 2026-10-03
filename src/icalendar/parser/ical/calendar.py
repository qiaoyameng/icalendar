"""Special parsing for calendar components."""

from .component import ComponentIcalParser


class CalendarIcalParser(ComponentIcalParser):
    """A parser for calendar components."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._register_embedded_timezones = True

    def prepare_components(self) -> None:
        """Prepare the parsed components."""


__all__ = ["CalendarIcalParser"]
