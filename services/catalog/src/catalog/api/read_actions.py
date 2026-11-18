"""Which actions only read: the one list the authorization router and the read-only window both judge by.

The Lance Namespace grammar puts the operation in the path (``POST /v1/<object>/{id}/<action>``), so a read arrives as
a POST and is told from a write by the last segment of the route it matches. The router asks which rung an action takes
(:mod:`catalog.api.fga_deps`); the read-only window and the signing gate ask whether it writes
(:mod:`catalog.api.maintenance_mode`). Both read the data and metadata sets here, so a spec action the router gives a
reader's rung is one the window serves; the vend door alone differs, a read to the window at its read tier only.
"""

from __future__ import annotations

from typing import Final


#: The actions that read a table's or a namespace's DATA: rows, or the means to read them. ``credentials`` is the vend
#: door, a data read at its read tier alone. ``changes`` is every row a table ever received, in order. ``explain_plan``
#: and ``analyze_plan`` take the rung their query would: analyze runs it, and a filter it is handed answers a yes/no
#: about the values.
DATA_READ_ACTIONS: Final = frozenset({"query", "count_rows", "credentials", "blobs", "changes", "explain_plan", "analyze_plan"})

#: The actions that read METADATA: what an object is, never its rows. ``version`` is a tag's version
#: (``tags/version``); ``tasks`` is what is scheduled against an object; ``history`` is the commit log, what changed
#: and when.
META_READ_ACTIONS: Final = frozenset({"describe", "exists", "list", "stats", "version", "tasks", "history"})

#: rask's management reads, served as POST in the same grammar, which take a rung of their own at their doors: the
#: estate FGA workbench's derivations, a caller's own permissions, a decision, and a maintenance preview.
MANAGEMENT_READ_ACTIONS: Final = frozenset({"my-permissions", "check", "graph", "preview", "list-objects", "list-users", "expand", "simulate"})

#: The route a table's credential is vended on. It reads at the read tier and writes at any other, which rides its
#: query; a route that merely ends in the same word (a trashed table's reclaim session) is not it.
TABLE_VEND_ROUTE: Final = "/management/v1/table/{id}/credentials"

#: Every action that only reads, the vend door aside (its tier decides).
READ_ACTIONS: Final = (DATA_READ_ACTIONS | META_READ_ACTIONS | MANAGEMENT_READ_ACTIONS) - {"credentials"}
