# chart/templates/openfga-model.yaml

Rationale for `chart/templates/openfga-model.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `apiVersion: batch/v1`

WRITE THE AUTHORIZATION MODEL ([[LH-174]]). `model.fga` is the source of truth for every `can_*` the
code derives, and until this hook existed nothing put it in OpenFGA — `rask-openfga-migrate` is the
DATABASE migration, and `make fga-test` diffs `model.json` against `model.fga`, two of three copies.
The store was the third and was checked by nothing.

WHAT THAT COST, measured 2026-09-18: the store's newest model defined 26/27/26 relations on
warehouse/namespace/table against 30/29/29 in the repo. `warehouse#maintainer` existed in the model
the code reasons about and could not be written, so `bootstrap-admin` — the hook two weights later —
CrashLooped on `Invalid tuple '…#maintainer@user:service-maintenance'` and wedged the release in
`pending-upgrade`, which refuses every later upgrade.

PRE-UPGRADE ([[LH-201]]): each service resolves its image's model at boot, so it must exist before new
pods start; `post-install` covers a first install. Either way it precedes `bootstrap-admin` (weight 5).

READ FROM THE IMAGE, never a ConfigMap. `service_kit/governed/auth/` ships `model.fga`,
`model.fga.yaml` and `model.json` inside the catalog image, so the hook reads the same bytes the
services import. Rendering it into a ConfigMap would have created a FOURTH copy of the artefact whose
copies had already drifted — the exact shape of this row.

IDEMPOTENT BY COMPARISON, not by blind write. An OpenFGA model is immutable and versioned, so writing
unconditionally would mint a new id on every upgrade and grow the store forever (50 models already).
It writes only a model the store has never held; a body held anywhere in it is not written again.

## `command: ["python", "-m", "service_kit.governed.auth.write_model"]`

A MODULE, not a heredoc. The comparison that decides whether to write is the part
that matters — write always and the store grows a model per upgrade, compare too
loosely and it never converges — and a script inlined here is covered by nothing.
`service_kit.governed.auth.write_model` lives beside the model it writes and has its
own suite.
