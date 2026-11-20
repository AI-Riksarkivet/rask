"""The auth plane: who a caller is, what it may do, and the trail of both, shared by every lakehouse service (ADR 0224).

Who a caller is: the bearer's verification (`oidc`, a person's identity provider in `idp_identity`, a workload's
ServiceAccount token in `machine_identity`, the principal both name in `principal`), the caller a bearer proves at a door
(`door_caller`), the judgement made on a request's headers before its body is read (`admission`), the settings a
service's gate is built from (`settings`), the issuer mirror's app (`issuer_mirror`) and the one rule deciding whether a
value names a person (`subjects`, named by this path in `docs/COMMIT-EVENTS.md`). What this pod presents as itself: the
projected token file (`projected_token`), the OTLP exporters' session (`otlp_identity`), its login to the secret store
(`secrets`) and its signing key (`signing_key`); the object-store key pair a store credential is carried as is the
store's (`lakehouse.storage.credential`). What a caller may do: OpenFGA (the six `fga_*` modules, one per
job: the model as data, ids, resilience, provisioning, reads and writes; the model the chart writes is under `auth/`), the
decision
seam (`decider`) and the wiring a service's lifespan runs (`auth_lifespan`). The trail: `audit`.

These modules are imported by the catalog, lineage and maintenance alike, and inside the library by the store and the
bus where they present the pod's identity; `.importlinter` names each importer. Nothing is exported here: an importer
names the module that defines what it uses.
"""
