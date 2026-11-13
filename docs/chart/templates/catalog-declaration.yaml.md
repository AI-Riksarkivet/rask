# chart/templates/catalog-declaration.yaml

Rationale for `chart/templates/catalog-declaration.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `kind: ConfigMap`

What git declares of the estate's tenants (XC-213, ADR 0206): the projects, warehouses and service grants under
`catalog.declared`, as one JSON document the catalog mounts at `/etc/rask/declaration/declaration.json`
(`LANCE_DECLARATION_FILE`) and converges the estate onto on its `catalog.declarationConverge` tick
(`services/catalog/src/catalog/api/declaration_converge.py`).

A ConfigMap and not an env value, for two reasons. The declaration can grow with the estate, and a mounted volume is
refreshed in place by the kubelet, so a changed declaration reaches the running catalog within about a minute and
rolls no pod: the tick reads the file each time it runs. The catalog is the reconciler because it is already the
authorization store's only client, so converging adds no workload, no ServiceAccount and no role.

It carries no secret: ids, principals and rung names only.

## `declaration.json: {{ include "lance.declaration" . | quote }}`

`lance.declaration` (`_helpers.tpl`) renders the three lists as the catalog's `Declaration` model reads them. A project
naming no `admins` is given `auth.bootstrapAdmin`, so the default project of a fresh install has an administrator. With
neither, the project is made with no admin, and one is declared later.
