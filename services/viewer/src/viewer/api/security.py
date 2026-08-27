"""Who is asking, and may they see this corpus.

The viewer shipped with no authorization at all: `GET /api/datasets` enumerated every corpus in the
registry — ids, table stats, declared capabilities — to any caller, and `/api/datasets/{id}/descriptor`
handed out a corpus's full schema. That was the documented "localhost / trusted network" posture,
and it stops being defensible the moment more than one person can reach the zone. A corpus LIST is
itself sensitive: it names data someone may not know exists.

**A corpus is not a new kind of object.** `MediaSettings.catalog_table_id(dataset_id, table)` already
maps a media dataset's table onto the catalog's identifier, and the annotator reads and writes Lance
through exactly that mapping. So the FGA object is the `table` the model already defines, with the
rungs it already has — no parallel `dataset` type to keep in step with the real one.

Everything mechanical (bearer → verified subject, the three-outcome checker) comes from
`service_kit.governed.deps`, shared with the annotator rather than copied out of it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from openfga_sdk.client import OpenFgaClient

from service_kit.governed.deps import FgaChecker, make_auth_deps
from viewer.core.config import ViewerSettings, get_viewer_settings


SettingsDep = Annotated[ViewerSettings, Depends(get_viewer_settings)]

_deps = make_auth_deps(SettingsDep)

CurrentSubject = Annotated[str, Depends(_deps.current_subject)]
CheckerDep = Annotated[FgaChecker, Depends(_deps.get_checker)]

#: The RAW client, for the FILTERING path. `CheckerDep` is one relation on one object by design and
#: cannot express a batch; see `AuthDeps.get_fga_client`.
FgaClientDep = Annotated[OpenFgaClient | None, Depends(_deps.get_fga_client)]

#: The relation a READ of a corpus's metadata requires. `can_get_metadata` and not `can_read_data`:
#: listing a corpus and reading its descriptor is metadata, and the model already separates the two —
#: gating the list on data access would hide corpora from someone allowed to know they exist.
READ_METADATA = "can_get_metadata"

#: The relation a read of actual BYTES requires (#90). Separate from `READ_METADATA` because the
#: model separates them and the difference is the whole point for an archive: knowing a volume of
#: sealed records exists is not the same permission as reading the pages. `/api/page` returns image
#: bytes, so it takes this rung; `/api/pages` lists metadata and takes the metadata rung, matching
#: how `datasets.py` reasons about a corpus listing.
READ_DATA = "can_read_data"

#: Raw OBJECT-STORE browsing (#90) — the S3 list/HEAD/download routes. An ESTATE-wide privilege,
#: checked ONLY against `fga_root_object`, exactly like the catalog's `can_observe_events`.
#:
#: Not a per-store grant, and that is a decision with a reason. A `store` FGA type would need a
#: parent tuple per store to be reachable, and the four SHIPPED default stores are never registered
#: through the API — they come from `DEFAULT_STORES` in code, so nothing would ever write their
#: tuples. The model would be correct and the gate would deny everyone including the estate owner.
#: A gate that denies everyone is an outage, not a gate. Per-store granularity lands the day store
#: registration owns its own tuples.
#:
#: Owner tier, because the registry's buckets include the external RAW tier and the observability
#: bucket — outside the medallion entirely (R23) — so a per-tenant admin must not reach them.
BROWSE_STORAGE = "can_browse_storage"


# The two object-naming rules (`corpus_object`, `table_object`) moved to `service_kit.media.authz`
# when the annotator's assist plane needed the same object. Import them from there — this module's
# own docstring is why they must not be written twice.
