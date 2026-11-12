"""The stamp a raise of a table's listing floor commits into its manifest config ([[LH-094]], [[LH-575]]).

The catalog commits it, at its maintainer door ``POST /management/v1/table/{id}/maintenance/floor``, and maintenance
reads it to tell whether a pass has cleaned beneath the last raise. Its value is the epoch the catalog committed it at,
on the clock that stamps the raise's own manifest, so the floor (the earliest retained manifest's time) passes it once
the versions beneath the raise are cleaned.
"""

from __future__ import annotations

from typing import Final


#: Namespaced, so an operator reading a table's config can tell the raise apart from a real commit. To the fraction, as
#: the floor is: a stamp cut to its second could read as passed by a manifest written earlier in it.
FLOOR_KEY: Final = "rask.maintenance.floor_raised_at"
