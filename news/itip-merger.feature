Add :mod:`icalendar.itip`, an in-memory iTIP (RFC 5546) merger that applies
authenticated ``REQUEST``, ``CANCEL`` and ``REPLY`` VEVENT messages to a
Calendar, handling whole-series and single-instance changes, SEQUENCE/DTSTAMP
versioning, retransmissions and stale replies, while returning per-message
applied/ignored/conflict/rejected reports.
