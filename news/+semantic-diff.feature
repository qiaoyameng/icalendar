Add :mod:`icalendar.diff` with ``diff_calendars`` and ``apply_patch``: a
semantic diff for calendars that locates components by ``UID``,
``RECURRENCE-ID``, and hierarchy position, ignores non-semantic
differences (name case, ordering, line folding), preserves repeated
properties, parameter lists, and unknown ``X-`` properties, and applies
patches atomically with JSON-serializable conflict reports when
preconditions fail.
