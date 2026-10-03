Add bidirectional conversion between calendar components and :rfc:`6321`
xCal XML: ``Component.to_xcal()``/``to_xcal_bytes()`` and
``Component.from_xcal()`` (also available as module-level
``component_to_xcal``, ``component_to_xcal_bytes`` and ``xcal_to_component``).
Structured values (period, recur, utc-offset and binary) use the nested
elements defined by RFC 6321, parameters and multi-valued properties follow
the xCal namespace schema, and unknown components and X-properties
round-trip. The XML parser uses the standard library only, disables all
external resources and applies depth and text size limits.
