# 0078. `GET /stage-runners` admits any signed-in caller (owner default, 2026-09-26)

**Decision.** The producer's stage-runner list is a deployment-config read: any caller
`produce_auth.admit_caller` admits gets it (`admit_config_read`), with no `?project=` and no project
check. This supersedes the 2026-09-25 entry above where it keeps `?project=` "on `GET /stage-runners`".

**Why no project.** The answer is `sorted(stage_runner_urls)`, rendered from
`medallion.stageRunners[].name`: one Deployment per runner, shared by every tenant, carrying no run
state and naming no project. A `can_administer` check on a caller-chosen project authorized nothing,
and it made the stage doors an oracle for the list: they read the run before authorizing, so a caller
with no grant anywhere could tell a configured runner (502 while it is down) from a made-up one (404).
The unknown-runner 404 names the configured runners to every admitted caller for the same reason.

**Lakekeeper gates its server-wide config the same way.** `GET /management/v1/info`
(`crates/lakekeeper/src/api/management/v1/server.rs:304-348`, commit a58e4017) refuses only an
anonymous actor when authn is enabled (`:309-318`) and makes no authorizer call for any other
(`:319`), while its answer includes deployment config such as the registered task-queue names
(`:341`). Its per-warehouse `GET /catalog/v1/config` is authorized per warehouse
(`crates/lakekeeper/src/server/config.rs:75`), because a storage profile is one warehouse's.

**Audited as the admission.** A person's read is `authn success`, as the catalog records an
authentication-only read; the service token's is `produce_service_token allow`, as every producer door
records its acceptance; both name the path, as the door's own public-caller refusal does. Pinned by
`test_the_operator_doors_authorize_on_the_resource.py` (the list, the oracle and the audit) and
`services/gateway/tests/test_lance_routes.py` (a public caller refused through the row).
