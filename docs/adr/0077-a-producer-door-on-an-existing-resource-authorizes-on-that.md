# 0077. A producer door on an existing resource authorizes on THAT resource (owner, 2026-09-25)

**Decision** ("Authorize on the resource"). `?project=` names a WRITE TARGET, so it stays on `POST /produce`
(and on `GET /stage-runners`, whose answer is the same for every tenant). A door that reads or stops
something that already exists authenticates only (`produce_auth.admit_caller`) and checks
`can_administer` on the project the resource records: a stage run's, as its hosting stage runner reads it
off the instance's trigger; a training watch's, from `TrainJobSpec.project`; a stalled cell's, one
`batch_check` over the DECLARED projects before anything is measured, so only the caller's own edges
are read (the single-tenant row, project `""`, is checked as the configured project). A training
door acts on `train_run` instances only: the producer also hosts `promotion_review`, whose door is
`/promotions/{id}` on `can_promote`. The service token's acceptance is audited on the project the
call acts on, not the configured one. Measured on e4e60b60: alice, admin of `project:mine` only,
got 200 on `GET /cascade/stalled?project=mine` listing acme's and other's cells, and `?project=mine` moved
the stage show/terminate gate the same way. This supersedes "Cascade repair" (2026-09-04) where it says "`terminate` stays
on `authorize_produce`": the rung is still `can_administer`, now on the run's own project. The service
token path is unchanged. `test_only_WRITE_TARGET_doors_take_a_caller_chosen_project` refuses a new door
that declares `?project=` until someone classifies it.

**403 on another tenant's run, not 404.** The catalog collapses "not yours" into 404 on its destructive
doors because warehouse and project ids are enumerable names. Stage and training ids are content hashes
(`stage-<submission hash>`), so a 403 confirms only an id the caller already held — and the direct
siblings, ingest's run doors and the promotion door, answer 403. The stalled read answers an admin of
nothing with an empty list, as ingest's cross-tenant listing does: a 403 for the whole call would say
that some tenant has something stalled.

**A run whose tenant cannot be read is refused to a person (503).** A stage runner that cannot parse the
input, or a build whose status body predates the field, names no project; reading that as single-tenant
would hand a tenant's run to the configured project's admins.

**`GET /authorize` is deleted.** Nothing called it: the jetstream and audit BFFs gate on the catalog's
`GET /v1/events` probe, so the 2026-07-23 `/streams` entry above describes a gate that is gone.
