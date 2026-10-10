# 0070. Four owner answers, and the reference implementation that unblocked three of them (owner, 2026-09-21)

Five of the six decision clusters holding [[open_backlog_left_new.md]]'s Phase 1 were put to the owner
as one question each. Three came back as rulings, and two came back as *"why is this even blocked"* —
which turned out to be right, and is the part worth recording.

**E · There is no production estate yet.** *"no not yet so we work with our locally dummies."* Seven
rows were each individually blocked on a prod-hardening decision — a certificate source ([[XC-007]]),
an OpenBao unseal mechanism ([[XC-006]]), the AGE→CNPG cutover ([[XC-008]], [[LH-108]]), which real IdP
prod federates to ([[XC-025]]), a signing-key custodian ([[XC-030]]) and where off-cluster pg dumps go
([[XC-013]]). They are one question, and its answer is "not yet". They are parked as LOW with that
reason, which drops Phase 1's HIGH count from 17 to 10. **Parking is not closing:** each keeps its
evidence, and the day a prod estate exists they return at the priority they had. What this ruling buys
is that the register stops presenting seven un-actionable rows as urgent work.

**B · The secrets rule is an OUTCOME, not a mechanism.** [[XC-002]] asked whether an ESO-written Secret
delivered by `secretKeyRef` satisfies *"never secret through envs"*, because the estate asserted both
answers: `.claude/skills/rask-dapr/SKILL.md` and `test_the_ray_credential_has_one_source.py` call it the
sanctioned path for a sidecar-less pod, while `test_secret_env_delivery_only_shrinks.py` counts every
`secretKeyRef` as the banned one. The owner's answer: *"The rule I set I['m] not sure it['s] hard [on]
how the secret will be delivered, my only concern is harden[ed] process regarding secret handling and
that we do zero trust idiomatic and best practices as well cloud native right way."*

So the gate was asking the wrong question. The one to ask is **does the workload hold a SECRET, or a
REFERENCE it exchanges at use time.** Read in source that day, Lakekeeper answers it that way: a
`SecretId` (an opaque UUID) is stored on the warehouse record, and a pluggable `SecretStore` trait
resolves it at use time behind a jittered-TTL, single-flight read-through cache
(`crates/lakekeeper/src/service/secrets.rs`). The credential is never in config — only its id is.

**And the reference implementation is weaker than rask's literal rule, which bounds the target
honestly.** `lakekeeper-secrets-kv2`'s `KV2Config { url, user, password, secret_mount }` is built by
figment from `LAKEKEEPER__`-prefixed environment variables — userpass auth against Vault, `#[redact]`ed
in logs and nowhere else. So the achievable posture is ONE bootstrap secret reaching the process, with
everything else a reference; zero is not what the state of the art does. rask already carries the field
this needs and does not use it: [[LH-129]] records `RASK_CREDENTIAL_REF` as having no consumer. rask's
STS vending (`vending.build_session_policy`, bucket+prefix, 900 s) has no Lakekeeper equivalent and is
ahead of it.

**A · The FGA model shape is a PORT, not a decision.** [[LH-055]]'s marker read as five open design
questions. The owner's answer was *"is [it] not that easy to look how they do it and try to do
similar? I don't understand [why] backward compatibility for the rules … does matter, you can just
delete old and make new"* — and on both points that is correct. Backward compatibility was never the
blocker (nothing here needs it), and Lakekeeper's model answers all but one sub-question in source
(`authz/openfga/v4.12/components/`, eleven modules, read 2026-09-21):

| the open question | what the reference model does |
| --- | --- |
| an `estate` type, or warehouse-as-root? | `type server`, with `define project: [project]` beneath it |
| a machine identity | `define operator: [user, role#assignee]` on `server`, *"designed to be used by machines that provision resources inside Lakekeeper, i.e. a `KubernetesOperator`"* — a RELATION on the top type, not a principal type. `type user` carries no relations at all |
| the security_admin / data_admin / role_creator split | all three on `project`, plus `tag_creator`; each is `[user, role#assignee] or <stronger>` |
| does `can_set_protection` split from `can_drop`? | **no — it collapses there too**: both resolve to `modify_effective` on table and on warehouse |
| column-level classification ([[LH-058]]) | **no `column` type exists.** The governance vocabulary is a TAG type with per-tag `apply` delegation, *"Independent of `modify` (separation of duties: classify without holding data/DDL rights)"* |
| branch-scoped governance ([[LH-056]]) | no branch type; `lakekeeper_catalog_tag` says in as many words that it is *"Distinct from an Iceberg snapshot tag"*, so snapshot refs are not FGA objects |

Three structural devices there have no rask equivalent and are what the port is actually for. **The
`_effective` twin:** every privilege is two relations — the bare name, which grants are written to and
listed from, and `_effective`, which adds what the privilege subsumes plus what flows down from the
parent; actions read the twin. That is what makes protection-and-drop sharing a tier a non-problem:
they are distinct ACTIONS over one effective tier, not one collapsed permission. **`visible_below`:** a
downward-only recursion (`describe or select or … or visible_below from child`) that answers list
filtering without a descendant ever re-asking its parent. **The grant/revoke asymmetry:** `can_grant_*`
admits delegation (`manage_grants or (modify_effective and pass_grants)`) while every `can_revoke_*` is
`manage_grants` alone — *"Taking a privilege back is administration, never delegation."*

Two of their modules answer rows filed elsewhere. `model_version.fga` pins which authorization-model
version a store is on as a TUPLE (`type auth_model_id`; `define exists: [auth_model_id:*]`) — which is
the shape [[LH-150]] wants for refusing a boot whose configuration disagrees with the tuples already
stored. And `role.can_update_source_system` — *"rebind the role's external identity (provider + source
id) to a different source system"* — is precisely the stable-internal-id-plus-rebindable-external-
binding design [[LH-063]] asks whether to build.

**D · DDL as a RunEvent is wrong by the spec, and the reference CONSUMER supports the fix only
partly.** [[LIN-004]] asked whether to move DDL off `RunEvent`. The OpenLineage spec settles the first
half: `DatasetEvent` is *"A Dataset sent within static metadata events"*, and the schema forbids the
`job` and `run` members on it outright (`"not": { "required": ["job", "run"] }`), so a catalog change
that no job performed is exactly what it is for. The second half is the caveat that was missing:
Marquez, the reference implementation, ingests `DatasetEvent` for real — `OpenLineageService
.createAsync(DatasetEvent)` persists it and calls `updateMarquezModel` — but `EventTypeResolver.java`
still carries `// FIXME: We are hardcoding the 'eventType' for now as we currently don't support static
lineage!`. So the move is correct and a standard consumer may still render it thinly. That is a known
trade now, not an open ruling.

**What is still genuinely the owner's, and it is one word.** The double-home cluster ([[LH-164]],
[[LH-016]], and through them [[LH-137]] and [[LH-092]]) is not a design question — no external project
speaks to it and `lance_docs/` does not either. It is data disposition: roughly 524 rows of
unregistered, ungoverned medallion data, to reap or to register.
