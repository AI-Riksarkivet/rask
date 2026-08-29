"""controlplane — a read-only view of the CLUSTER's `platform.rask.io` Project CRs.

**It is not the source of the estate's project list, and this docstring used to imply it was**
("for the home picker"). The home zone's project gallery reads TENANTS from the catalog —
`me.projects` off the frozen `/v1/me` contract, plus `/capi/v1/projects` for an estate admin
(`home/src/lib/gallery.ts`) — and has done since the picker was replaced. Three unrelated things are
called "project" (catalog tenant, k8s CR, annotator labeling project); this service owns only the
middle one, and nothing joins it to the other two.

The CRs it reads are published by `rask-operator`, a SEPARATE repo. This chart deliberately does not
ship that CRD (`docs/DECISIONS.md`, *"Watch enrolment does not wait for the `platform.rask.io` CRD"*,
2026-08-16): installing it without its controller would yield unreconciled CRs that render as
projects stuck mid-provision. On an estate without the operator this service therefore has nothing
to list and says so in those words — `501`, naming the unregistered resource type, never an empty
`200` (see `routes.py`).

Stateless: no DB/Lance/Ray/S3; reads the k8s API per request."""

from controlplane import health, routes
from controlplane.lifespan import make_lifespan
from service_kit import make_service_app


app = make_service_app(title="controlplane", routers=[health.router, routes.router], lifespan=make_lifespan)
