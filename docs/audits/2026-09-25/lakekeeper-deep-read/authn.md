# Lakekeeper deep-read: AUTHENTICATION (machine + human) and how rask should solve its identity rows

Date 2026-09-25. Read-only on `/home/gabriel/Desktop/rask`; the cluster was read with `get`, `logs`, `get --raw` and one
anonymous `curl`, and nothing was written to it. Lakekeeper ref = `/home/gabriel/Desktop/lakekeeper-ref` (its `limes`
dependency is pinned at `vakamo-labs/limes-rs@a88da41`, `Cargo.toml:117`, and was fetched into
`scratchpad/reaudit/lakekeeper/limes/` because it is where Lakekeeper actually does the token work). Chart ref =
`scratchpad/reaudit/lakekeeper-charts/charts/lakekeeper`.

Owner rulings this report works inside: **D1** (machine identity = K8s ServiceAccount tokens, via TokenReview or the SA issuer
in the OIDC verifier; identity comes from the verified token and no header asserts it), and **D5** (principal key =
`<idp-id>~<claim>`). This report does not re-ask either. It answers **how**, and it raises two sub-questions the audits got wrong
(§0).

---

## 0. Headline, and three corrections to today's audits

1. **Lakekeeper's K8s principal is `kubernetes~<ServiceAccount UID>`, not `k8s~system:serviceaccount:<ns>:<sa>`.** limes'
   `KubernetesAuthenticator` sets `subject = user.uid` from the TokenReview status, and `name = user.username`
   (`limes/kubernetes.rs:33-38,196-200`, pinned by its own test at `:300-307`). Lakekeeper's docs contradict its code in two
   places: `docs/docs/authentication.md:543` says `k8s~<namespace>~<service-account-name>`, and `config.rs:457` gives
   `kubernetes~system:serviceaccount:lk:op` as an instance-admin example. With the code as written, that example can never match.
   `findings_lance_lakekeeper.md` row 74 recommends `user:k8s~system:serviceaccount:<ns>:<sa>`, which is neither Lakekeeper's shape
   nor, as the next point shows, something OpenFGA will store.
2. **OpenFGA refuses a user or object id with more than one `:`.** In `openfga/openfga@v1.18.3` (the deployed image,
   `kubectl get deploy rask-openfga` → `openfga/openfga:v1.18.3`), `pkg/tuple/tuple.go:416-438` says a valid object contains
   **exactly one `:`**. So `user:kubernetes~system:serviceaccount:default:rask-sa-catalog` is not a valid tuple subject.
   Lakekeeper never meets the problem in its K8s case, because a UID has no colon, and in general it URL-encodes every principal
   before handing it to FGA: `format!("user:{}", urlencoding::encode(&self.to_string()))`
   (`crates/authz-openfga/src/entities.rs:156-159`). rask writes `f"user:{user}"` unencoded at four sites
   (`packages/service-kit/src/service_kit/governed/fga.py:832,877,962,1750`). **D5 therefore needs an encoding seam, or a
   colon-free claim. It cannot take a raw K8s username.**
3. **No Lakekeeper "boot validator refuses issuers that shadow one another".** `findings_lance_lakekeeper.md` row 74 cites one at
   `authn.rs:...934-1000`, but those lines are tests of chain ORDER. What Lakekeeper actually does:
   - it orders the chain deterministically: primary `oidc` first, then the extras sorted alphabetically, then K8s
     (`authn.rs:248-256,293-301`);
   - it validates IdP-id grammar and reserved names (`config.rs:151-179`);
   - it routes by first match on the unverified `iss`/`aud` (`limes/chain.rs:53-63`).

   Two providers with overlapping issuers are **not** refused; the first one wins. rask should do better and refuse at boot (§T2).
4. **Also: its own row cites, re-read.** The line ranges row 63 gives for `authn.rs` (`:299-363,684-760`) do not match the current
   file. The K8s code is at `authn.rs:159-215` and `UserId` at `:653-728`.

What rask should build is one identity seam with per-provider config. It has three providers:
- `kubernetes`: the SA issuer, verified offline through JWKS;
- one per Dex connector: an operator-named id, keyed on the upstream user id;
- nothing else.

Every principal is `<idp-id>~<claim>`, validated, and percent-encoded at the single FGA boundary. The static service door
(`x-lance-service-identity` + the shared app token, or `service-token-<id>`) is deleted, and the allowlists become FGA tuples.
§T1–T12 give the details, and §3 gives the order in which to do them.

---

## 1. Live measurements (2026-09-25, read-only)

| # | What | Result |
|---|---|---|
| M1 | `kubectl get --raw /.well-known/openid-configuration` | `issuer: https://kubernetes.default.svc.cluster.local`, `jwks_uri: https://10.16.51.53:6443/openid/v1/jwks` (a node IP, not under the issuer), `id_token_signing_alg_values_supported: [RS256]` |
| M2 | `clusterrolebinding system:service-account-issuer-discovery` | bound to `Group system:serviceaccounts` only |
| M3 | anonymous `curl -k` to `:6443/.well-known/openid-configuration` and `/openid/v1/jwks` | **401 / 401**. The only binding to `system:unauthenticated` is `system:public-info-viewer` |
| M4 | server version | `v1.36.2+k3s1` |
| M5 | pod `spec.serviceAccountName` | lakehouse pods (catalog, lineage, maintenance ×2, medallion-producer, three stage runners, dex, openbao, nats, age, minio, ray heads, viewer, search, annotator) run as **`default`**. Fleet pods (gateway, ingest, notifications, compute, flows, seven web zones) run as **`rask`**. The chart gate is `security.serviceAccounts.enabled: false` (`chart/values.yaml:727-728`, used at `chart/templates/services.yaml:38-40`) |
| M6 | catalog audit lines in its retained log (12:44–12:56) | 3,387 `authn success subject='service-maintenance'`, and **819 `authn failure reason='service_credential'` with `subject=''`**. The preceding access lines come from `10.42.0.123` (= `rask-medallion-producer`), e.g. `GET /v1/table/*-bronze%24events/tags/list 401`. **Cause NOT diagnosed**, and the refusal record carries no claimed identity (`services/catalog/src/catalog/api/security.py:141-145`), so the log cannot say who was refused. This is the same class `cascade_lag_readers.py:91-114` records as "2,700 401s … in twenty-five minutes" on 2026-09-04 |
| M7 | OpenFGA | `openfga/openfga:v1.18.3`. `IsValidObject` allows exactly one `:` (`pkg/tuple/tuple.go:416-438` at tag v1.18.3) |
| M8 | pylance 12.0.0 (installed source, not run) | `lance.namespace.DynamicContextProvider` is "called synchronously before each namespace operation" (`.venv/.../lance/namespace.py:136-183`), and `RestNamespace(context_provider=...)` takes it (`:968-988`). rask uses it nowhere: grepping `services packages scripts runners` for `DynamicContextProvider` or `context_provider=` finds nothing |

---

## 2. Topics

### T1 — Kubernetes ServiceAccount authentication (TokenReview, audiences, legacy tokens)

**Lakekeeper**
- `enable_kubernetes_authentication` builds `KubernetesAuthenticator::try_new_with_default_client(Some("kubernetes"), audiences)`.
  A failure is fatal at boot: "there's only ever one cluster … we fail closed via `?`" (`authn.rs:161-182`).
- Legacy tokens are opt-in and pinned to the issuers `kubernetes/serviceaccount` and
  `https://kubernetes.default.svc.cluster.local` (`authn.rs:184-207`, `config.rs:418-420`).
- limes POSTs a `TokenReview{spec.token, spec.audiences}` on **every** request, with no cache (`limes/kubernetes.rs:131-147`). It then:
  - validates the status audiences (`:181-183,229-242`);
  - refuses on `status.error`, a missing `user`, or a missing `uid` (`:186-198`).

  It never reads `status.authenticated` explicitly. rask should not copy that gap: check `authenticated is True`.
- Principal type is always `Application` (`:211`).
- With **no** audience set, `can_handle_token` claims every JWT (`limes/kubernetes.rs:22-26,150-166`). That is why the chart says
  the K8s audience "must be specified" when OIDC is also on (`values.yaml:535-537`), although nothing validates it (the
  `_validations.tpl` grep is empty).
- The chart grants `system:auth-delegator` through a ClusterRoleBinding (`templates/serviceaccount.yaml:60-79`, the default
  `createClusterRoleBinding: true` at `values.yaml:543`).
- Clients just send the mounted token as `Authorization: Bearer` (`authentication.md:507-541`).

**rask today**
There is no K8s identity. A machine authenticates through the "service door":
- The caller sends `dapr-api-token` + `x-lance-service-identity`, and the door checks an allowlist, then the credential
  (`packages/service-kit/src/service_kit/governed/dapr_auth.py:457-513`).
- Non-privileged subjects share the one estate-wide app token (`:511-513`). Privileged subjects need `service-token-<id>` read from
  OpenBao through Dapr, cached for the life of the process (`:393-454`).
- The catalog then **mints a synthetic** `IDToken(iss="rask://service-door", exp=now+60, service=True)`
  (`services/catalog/src/catalog/api/security.py:41-47,152-169`).

Seven client sites build that header pair:
- `ingest/service_identity.py:71`
- `medallion/services/catalog_register.py:127`
- `medallion/services/cascade_lag_readers.py:91-114`
- `notifications/api/reconciler.py:246`
- `maintenance/services/catalog_identity.py:58`
- `lineage-kit/emitter.py:124`
- the web BFF `frontend/packages/api/src/bff.ts:195-201`

Three chart lists must agree, and they have drifted with measured outages: `lance.allServiceIdentities`
(`chart/templates/_helpers.tpl:1797-1824`, whose comment records the 2026-09-24 notifications 401 loop), `LANCE_SERVICE_SUBJECTS`
(`services.yaml:307`) and `LANCE_PRIVILEGED_SUBJECTS` (`:398`), plus the lineage twins (`:802`). The gateway has to strip every
header that could assert identity (`services/gateway/src/gateway/__init__.py:76-100`). M6 shows the model failing live today.

**rask should (on its stack)**
- Adopt the model. Choose **JWKS offline verification through the SA issuer**; TokenReview is the fallback D1 also allows.
  Four reasons:
  - One mechanism serves all three verifiers D1 names (catalog/lineage in Python, OpenFGA `authn.method=oidc`, MinIO's OpenID
    STS). TokenReview only serves rask's own code.
  - limes' TokenReview is an API-server round trip on every request (`kubernetes.rs:131-147`). The catalog's hot path should not
    inherit that.
  - The default discovery binding already covers every ServiceAccount (M2), while TokenReview needs `tokenreviews:create` cluster-wide.
  - `docs/DECISIONS.md:303-305` already rules "every service verifies signatures locally (JWKS, cached)".

  What JWKS costs: no immediate revocation when a pod or SA is deleted. That window is bounded by `expirationSeconds: 600`, the
  minimum a projected token allows. **Name the audience per verifier** (`rask-catalog`, `rask-lineage`, `openfga`, `minio-sts`) so a
  token lifted from one hop cannot be replayed at another. Lakekeeper's single-service analogue is "we recommend to specify the
  audience in all deployments" (`authentication.md:11`).
- The SA-issuer provider in rask's verifier must:
  - fetch discovery and JWKS with the pod's **own** SA token and the kube CA (`/var/run/secrets/kubernetes.io/serviceaccount/{token,ca.crt}`),
    because anonymous gets 401 (M3);
  - accept a `jwks_uri` that is not under the issuer (M1: node IP `10.16.51.53:6443`). `oidc.py:250-253` uses it as advertised,
    which is correct, but whether a pod can reach it was **not checked** (no exec);
  - keep the RS256 intersection (M1, `oidc.py:181-194`);
  - require `kubernetes.io.namespace` == the release namespace.
- For OpenFGA and MinIO: both fetch discovery without a bearer, so P5.3(a) needs one of two things. Either bind
  `system:service-account-issuer-discovery` to `system:unauthenticated` (a cluster-scoped binding that publishes only public keys),
  or put a JWKS mirror in front. M3 settles the anonymous half of P5.3(a): **refused today**.
- Per-service ServiceAccounts: delete the `security.serviceAccounts.enabled` gate, with no toggle (no-compat rule). Set
  `automountServiceAccountToken: false` plus an explicit projected volume (`audience`, `expirationSeconds: 600`). Clients re-read
  the file on each request or cache it for at most 60 s. This matters because `_secret_bundle`'s process-lifetime cache
  (`dapr_auth.py:393-413`) is the wrong shape for a rotating credential.
- Delete, with no shim: `x-lance-service-identity`, `service_principal`, `ServiceIdentity`, `SERVICE_DOOR_ISSUER` and the synthetic
  token, `LANCE_/LINEAGE_{SERVICE,PRIVILEGED}_SUBJECTS`, and the gateway strip of that header. `service-token-<id>` survives
  **only** as the LH-064 HMAC key (consistent with the reconciliation's D1(a)).
- **RED first:**
  - pod A's projected token cannot authenticate as B;
  - a token with audience `rask-lineage` is refused by the catalog;
  - the shared app token plus a claimed name answers 401;
  - a `kubernetes` token from another namespace is refused.

**Rows:** LH-079, LH-129, CP-001, CP-007, CTL-002, CTL-021, XC-004, LH-160, XC-002, LH-064 (key survives as HMAC only), and new:
"the service door is a static bearer plus an asserted header" (`findings_lance_lakekeeper.md` row 63).

---

### T2 — Multi-issuer OIDC: per-provider config, routing, chain order

**Lakekeeper**
- `OidcProviderConfig{uri, audience[], additional_issuers[], scope, subject_claims[], roles_claim, require_connected_on_startup=true}`,
  keyed by the IdP id (`authn.rs:100-138`; env `LAKEKEEPER__OPENID_PROVIDERS__<ID>__*`, `config.rs:434-438`).
- The primary `OPENID_PROVIDER_URI` becomes the idp `oidc` and is always required (`authn.rs:278-291`).
- Each provider refreshes its own JWKS on a 1 h TTL (`:352-357`) and applies its own audience, issuers, scope and subject claims
  (`:359-394`).
- The chain routes on unverified `iss`/`aud`. The first authenticator whose `can_handle_token` is true decides, with no fall-through
  (`limes/chain.rs:53-63`, `limes/jwks.rs:284-300`).
- Order is deterministic (`authn.rs:248-256,293-301`, pinned by tests `:941-970,972-1026`).
- An identity-continuity warning: moving the primary provider under another id re-keys every grant (`authentication.md:505`).

**rask today**
- `OIDCVerifier` accepts a list of issuers and routes on an exact `iss` match (`packages/service-kit/src/service_kit/governed/oidc.py:21-23,293-308`),
  but it takes **one audience for all of them** (`:153,169,319`) and silently de-duplicates issuers (`:165`).
- Settings expose **one** issuer string and one audience (`settings.py:173-174`). `attach_auth` passes that single string
  (`auth_lifespan.py:201-211`), and the chart feeds Dex's `issuer` / `clientId` to every service (`chart/templates/_helpers.tpl:1293-1299`).
- The multi-issuer capability is therefore dead in deployment, and it cannot carry a second audience.

**rask should**
- Replace `RASK_OIDC_{ISSUER,AUDIENCE,DISCOVERY_URL}` with a provider map: one mounted YAML, or `RASK_AUTH_PROVIDERS__<ID>__*`.
  Each entry carries `{idp_id, issuer, audiences[], subject_claims[], discovery_url, required, ca_file, fetch_token_file}`.
  Hard rename, no aliases, and `RETIRED_AUTH_ENV_NAMES` (`settings.py:47-65`) grows to refuse the old names.
- Keep rask's exact-`iss` routing, which is stricter than limes' substring `iss.contains(i)` (`jwks.rs:295`).
- **Refuse to boot** on:
  - two providers with the same issuer (the de-duplication at `oidc.py:165` would otherwise merge two idp-ids);
  - a provider with no audience;
  - zero providers while auth is enabled.
- Keep rask's local asymmetric algorithm allowlist (`oidc.py:64-75,181-194`). It is stronger than limes, which trusts the token
  header's `alg` whenever the JWK carries none (`limes/jwks.rs:332-336`).

**Rows:** LH-063, XC-025, P8.6/P8.7 foundation (new).

---

### T3 — Principal id `<idp-id>~<claim>` (D5), validation and FGA encoding

**Lakekeeper**
- The separator `~` (`authn.rs:30`). `UserId::try_new` requires an idp id (`:664-674`); the subject must be non-empty, under 128
  characters, and free of control characters (`:692-727`).
- Parsing splits on the **first** `~` (`:759-772`, limes `subject.rs:64-77`), so `oidc~a~b` → (`oidc`, `a~b`) (tests `:1238-1295`).
- The IdP-id grammar is `[a-z0-9-]+`, with `oidc` and `kubernetes` reserved (`config.rs:151-179`, tests `:1809-1862`). The same
  grammar is re-checked on the authenticator's ids at serve time (`serve.rs:198-202,644-668`).
- Serde round-trips the string form (`authn.rs:788-805`).
- FGA sees `user:` + URL-encoded id (`authz-openfga/src/entities.rs:156-159`).

**rask today**
- The FGA subject is the bare `token.sub` (`packages/service-kit/src/service_kit/governed/deps.py:172-192`, at `:190`).
- There are 140 `token.sub` reads in 32 non-test files (grep, matching the reconciliation's P8.4 count).
- No idp id and no validation beyond "`sub` present" (`oidc.py:329`).
- The FGA string is unencoded (`fga.py:832,877,962,1750`).
- Humans and services share one namespace (row 74).

**rask should**
- One `Principal(idp_id, subject)` type in `service_kit.governed`: `str()` = `<idp_id>~<subject>`, and a `to_fga()` that
  percent-encodes it (required by M7).
- The verifier returns it. `current_subject` returns `principal.key` and **every** `token.sub` consumer moves to it, with no
  overloading of `sub` (no-compat rule). P8.4's cheaper route, having the verifier write the key into `sub`, works too, but it
  erases the raw claim that audit and debugging need.
- Validate as Lakekeeper does. A 128-character subject keeps `user:<encoded>` inside OpenFGA's 512-byte user limit
  (`openfga.proto` `TupleKey.user max_bytes: 512`, lines 161-172; object 256, lines 186-196).
- Seed and admin lists in the chart are parsed as `Principal` at boot, and a bare name is refused (Lakekeeper rejects an id with no
  idp prefix, `config.rs` test `:1321-1336`).
- **K8s claim sub-question (needs one line from the owner):**
  - Lakekeeper keys on the SA **UID** (§0.1). It is colon-free and survives name reuse, but it cannot be known at render time, so the
    chart could not seed the service tuples declaratively.
  - The alternative is the JWT `sub` `system:serviceaccount:<ns>:<sa>`, percent-encoded. It is knowable at render time; re-creating
    an SA with the same name is the same principal, which needs namespace-admin, a power that already dominates.

  **Recommend the username, encoded**, recorded as a deliberate deviation from Lakekeeper's UID.
- **Dex translation:** Dex brokers several connectors behind **one** issuer, and its `sub` packs connector + user. So the Lakekeeper
  rule "one issuer → one idp-id" becomes **one Dex connector → one operator-named idp-id** here:
  - map `federated_claims.connector_id` → idp-id in config, and key on `federated_claims.user_id`;
  - that needs the `federated:id` scope, which the BFF does not request today (`frontend/packages/api/src/bff.ts:72`);
  - it needs a nested claim path, which limes lacks (`jwks.rs:507-519` reads top-level claims only);
  - renaming a connector then means editing the mapping, not re-keying.
- A **RED test first:** a connector rename keeps grants (LH-063's closes-when). Re-key the existing tuples, the notifications
  InboxActor ids and the lineage `onBehalfOf` subjects; current state is test data, so a reseed is acceptable.

**Rows:** LH-063, LOW-026 (identity triple, touched only), CTL-022 (subject erasure keys on the same id).

---

### T4 — Subject-claim selection

**Lakekeeper:** an ordered `subject_claims`, defaulting to `["oid","sub"]` because Entra's `sub` is per-application
(`authn.rs:76-82,374-389`; `authentication.md:21-27`). A single configured claim must be present (`authentication.md:23`), and the
first claim present wins (`limes/jwks.rs:507-519`).

**rask today:** `sub` only, and required (`oidc.py:329`).

**rask should:** make `subject_claims` per provider, as an ordered list of dotted paths:
- `kubernetes` → `sub`;
- each Dex connector → `federated_claims.user_id`;
- a future Entra or Keycloak provider → `oid` / `sub`.

No estate-wide default: Lakekeeper itself logs "Set `subject_claims` explicitly in production" (`authn.rs:378-382`).
**Rows:** LH-063, XC-025.

---

### T5 — Startup fail-closed checks

**Lakekeeper:**
- a required provider that fails discovery aborts boot (`authn.rs:323-334`);
- an optional one is skipped **for the process lifetime** (`:335-341`);
- boot is refused if every provider was skipped: "Refusing to start with authentication disabled" (`:236-246`, test `:1092-1124`);
- K8s failure is always fatal (`:161-178`);
- IdP ids are validated at config load (`config.rs:72,151-179`) and again at serve (`serve.rs:198-202,644-668`);
- instance admins must parse as `UserId` (`config.rs:462-463`);
- trusted-engine identities are asserted non-empty (`config.rs:93-119`).

OSS still boots with **no** authenticator after a warning ("not suitable for production", `authn.rs:257-261`); `authn_enabled()` is
the only switch (`config.rs:1092-1097`).

**rask today:**
- **Stricter than OSS** on the zero case: `assert_authentication_configured` refuses to boot on the ambiguity
  (`settings.py:223-249`, called at `catalog/main.py:98-101`, `lineage/main.py:61`, `medallion/producer.py:68`).
- It also refuses OIDC-on without issuer and audience (`:191-192`), an `http://` issuer (`:201-202`), FGA without OIDC
  (`:216-220`), and a Dapr door with no app token (`dapr_auth.py:272-299`).
- **But discovery failure is never fatal.** `warm()` returns failures and never raises (`oidc.py:263-291`), and `attach_auth` only
  logs them (`auth_lifespan.py:221-230`), even for `fatal=True` services: the catalog (`catalog/main.py:130-142`), lineage and the
  medallion.
- That contradicts the module's own claim that those services "crash on boot; that is deliberate and it is the only signal an
  operator watches" (`auth_lifespan.py:28-31`). A catalog with a wrong issuer boots green and answers 503.

**rask should**
- Add `required` per provider, default **true**. On a `fatal=True` service a required provider's warm failure raises at **boot**.
  That is not readiness: rask's argument against readiness gating (`oidc.py:271-275`) stays right, and Lakekeeper's check is
  boot-only too.
- `kubernetes` is always required, since the API server that scheduled the pod is reachable by definition.
- A non-required provider (e.g. an external IdP) is logged and **retried lazily**, which rask already does through `_resolve`'s TTL.
  That beats Lakekeeper's drop-for-process-lifetime on resilience (criterion 5).
- Refuse when zero providers built.
- Also refuse at boot: an idp-id outside the grammar or reserved; two providers with one issuer; any seeded principal that does not
  parse.
- **RED:**
  - a catalog whose required issuer answers 404 exits non-zero;
  - a catalog whose only optional provider is down boots and 503s that provider's tokens only.

**Rows:** XC-017 (these become checklist entries), new "OIDC discovery failure is never fatal even on `fatal=True` services".

---

### T6 — Where authentication sits in the request path

**Lakekeeper**
- The auth layer is `option_layer` and **absent when no authenticator is configured** (`api/router.rs:127-140`). It wraps
  `/catalog/v1`, `/management/v1` and `/lakekeeper/v1` (`:142-165`); `/health` is added **after**, so it is unauthenticated
  (`:166-173`).
- Request metadata is built in an outer layer (`:187-190`, `request_metadata.rs:576-639`).
- The middleware answers:
  - a missing header → 401 `MissingAuthorizationHeader`;
  - a bad token → 401 `AuthenticationFailed` (`authn.rs:420-439`);
  - an invalid subject → 400 (`:440-445`).
- `/management/v1/info` refuses anonymous callers when authn is on (`management/v1/server.rs:308-320`).
- `Authorization` is marked sensitive (`router.rs:194-196`), and logging it is an off-by-default debug flag (`config.rs:802-810`,
  `router.rs:202-204`).

**rask today**
Router-level `CurrentToken` (`catalog/api/security.py:188-190`) plus the shared `make_auth_deps` (`deps.py:144-192`) are already
the right shape. The earlier audit found authentication enforced on every catalog route (`findings_lance_lakekeeper.md:36`).

Two defects:
- the catalog's own `authenticate` catches `Exception` and audits a **verifier outage as `invalid_token`**
  (`catalog/api/security.py:179-183`), where the shared version splits it out (`deps.py:157-165`);
- service-door refusals are audited **without the claimed identity** (`security.py:141-145`), which is why M6's 819 refusals are
  undiagnosable from the log.

Spans copy headers from an allowlist (`service_kit/otel.py:29-35`), which is better than Lakekeeper's denylist layer.

**rask should**
- Delete the catalog's forked `authenticate`. Once the service door goes (T1), the catalog can use `make_auth_deps` like every other
  service; one door, per rask's own rule.
- Audit refusals with `idp_id` and the claimed subject when the token parses. Lakekeeper boxes the limes error into the problem
  body (`authn.rs:432-436`); rask should put the reason in the audit record and not in the response.

**Rows:** XC-017, LH-075 (audit stream), new: "the catalog audits a verifier outage as invalid_token" and "service-door refusals
carry no identity".

---

### T7 — Machine vs human discriminator; actors, assumed roles, delegation

**Lakekeeper**
- `Actor::{Anonymous, Principal(UserId), Role{principal, assumed_role}}`, plus the in-process `LakekeeperInternal`
  (`authn.rs:33-49`).
- A role is assumed by the explicit `x-assume-role` header (`:31,556-605`), resolved and **authorized and audited** as `assume_role`
  (`:514-545`).
- Instance-admin membership applies only to `Actor::Principal`: "role assumption is an explicit opt-in to a narrower scope"
  (`authn.rs:466-478`, `request_metadata.rs:174-191`). It bypasses control-plane actions only, never `ReadData` / `WriteData`
  (`config.rs:444-463`, `request_metadata.rs:284-308`), and each decision is stamped with a `PrivilegeSource`
  (`request_metadata.rs:65-88,272-282`).
- K8s principals are always `Application` (`limes/kubernetes.rs:211`); OIDC principals are classified from claims
  (`limes/jwks.rs:393-398`).

**rask today**
- "Is this a machine" is decided by `token.iss != SERVICE_DOOR_ISSUER` (`catalog/api/fga_deps.py:1225-1230`) and by the synthetic
  `service` claim (`catalog/api/v1/endpoints/publication.py:105-111`, where a service may name another subject: delegation).
- LH-064 adds a declared `on_behalf_of` inside the HMAC-signed facet (row text).
- The web BFF makes every **anonymous** visitor act as the zone's service identity for GETs (`bff.ts:153-201`). That is an implicit
  role assumption, with no audit of whom it was done for.

**rask should**
- The machine discriminator becomes `principal.idp_id == "kubernetes"`, which is what the token **is**, as Lakekeeper classifies it.
- Delegation stays explicit and declared (LH-064's `on_behalf_of`), which is Lakekeeper's assumed-role principle: an explicit,
  authorized, audited opt-in.
- The BFF's anonymous-as-service fallback should go. A missing session is a 401, as Lakekeeper answers a missing header
  (`authn.rs:420-427`). The alternative, only if the owner wants public reads, is an explicit `anonymous` principal with its own FGA
  grants. **No existing row** tracks this.

**Rows:** LH-064, LH-194 (a machine's own registration), new: "the web BFF acts as the zone's service identity for every anonymous
read".

---

### T8 — API keys and token issuance (the `x-api-key` principal)

**Lakekeeper:** "does not issue API-Keys or Client-Credentials itself" (`authentication.md:6`). It treats Iceberg's
`/v1/oauth/tokens` as unsupported (`api/iceberg/mod.rs:316-317`, `api/endpoints.rs:531-532`); the router trait exists
(`api/iceberg/v1/oauth.rs:1-35`) but nothing mounts it. Machines use their IdP's client-credentials flow or K8s tokens
(`authentication.md:29-30,507-541`).

**Lance spec:** `security:` is a disjunction of `OAuth2 | BearerAuth | ApiKeyAuth` (`lance_docs/ns_catalog/spec.yaml:81-84`). The
OAuth2 scheme names only a `tokenUrl: /oauth/token` (`:6729-6734`) and **no path** implements it (a grep of spec.yaml finds only
that line). `Identity.auth_token` → `Authorization: Bearer` and `api_key` → `x-api-key` are both marked "REST NAMESPACE ONLY"
(`:2451-2470`). Bearer alone is therefore conformant.

**rask today:** `x-api-key` is read nowhere, and no `/oauth/token` exists (grep); audit B6 still prescribes a key store (LH-079).

**rask should:** go bearer-only and record it in `docs/DECISIONS.md`; rewrite B6 as a conformance note citing `spec.yaml:81-84`
(this is P8.6's text). **Rows:** LH-079.

---

### T9 — Token lifetime and client refresh (machine clients, Lance clients)

**Lakekeeper:** JWKS keys refresh on a 1 h TTL (`authn.rs:352-357`). It recommends human tokens of at most one day
(`authentication.md:41`). K8s clients read the mounted token file (`authentication.md:527-533`).

**rask today:**
- Credentials are resolved once and cached for the process lifetime (`dapr_auth.py:393-413`, "rotating a dedicated credential means
  a rollout").
- No rask code builds a Lance REST namespace client, and none uses `DynamicContextProvider` (M8).
- The BFF records `expiresAt` and does not refresh (`frontend/packages/api/src/oidc.ts:106-117,168`).

**rask should:**
- Every outbound machine client reads the projected token file per request, with at most a 60 s cache, through one
  `service_kit` helper that replaces `outbound_app_token` / `dedicated_token_for`.
- For BYO or Lance clients, the Lance-idiomatic hook is a `DynamicContextProvider` returning `{"headers.Authorization": "Bearer …"}`
  per operation (M8; header prefix per `lance_docs/ns_catalog/namespace/supported-catalogs/lance-rest.md:17`). **Not run**: read
  from the installed source only.

**Rows:** LH-129, CP-001, CTL-021.

---

### T10 — North-south and sidecar hops (what `dapr-api-token` is FOR)

**Lakekeeper:** every request carries a bearer the service itself verifies, and there is no edge-minted credential
(`router.rs:127-165`).

**rask today:**
- `dapr-api-token` proves "arrived via the sidecar", never "trusted caller" (`dapr_auth.py:34-44`), yet the service door uses it
  as an identity credential (`:494-513`).
- CTL-002 asks what the edge should mint once kgateway calls Services directly.
- CTL-021: daprd overwrites `dapr-api-token` on invocation.

**rask should:**
- Keep `dapr-api-token` **only** on sidecar-delivered routes: pub/sub, bindings and actors (`require_dapr_token`, `:213-269`;
  `guard_actor_routes`, `:521-569`).
- Once T1 lands, east-west and north-south HTTP authenticate by bearer: the user's forwarded token, or a caller's projected SA token.
  The edge then mints nothing, which dissolves CTL-002's premise.
- For CTL-021, the reconciler sends its own SA token (audience `rask-lineage`) as `Authorization`. **Unverified**: that daprd
  service invocation passes `Authorization` through untouched. Measure it before relying on it; the fallback is direct HTTP, as
  ingest already does.

**Rows:** CTL-002, CTL-021, XC-004.

---

### T11 — Principal records: auto-provisioning and removal

**Lakekeeper:** users are auto-registered from token claims on the first authenticated call (`authentication.md:13-20,142`).
`parse_create_user_request` separates self-provisioning, which may use token data, from provisioning someone else, which needs
`ProvisionUsers` and may not use token data (`management/v1/user.rs:227-297,299-368,573-583`). Deleting a user removes that user's
assignments and FGA tuples (`:525-570`). Bootstrap makes the caller the first admin (`management/v1/server.rs:186-263`).

**rask today:** no principal record; users exist only as FGA tuples (`findings_lance_lakekeeper.md` row 75).

**rask should:** take the behaviour, not the Postgres shape: a control-root `_principals/<hash(key)>.json` written on first
authenticated call. It carries idp_id, kind (derived from idp_id), the display claims, and `deleted_at`. Add a subject-wide revoke
door. It keys on the D5 principal, so it lands after T3.

**Rows:** LH-063, CTL-022.

---

### T12 — Roles from token claims (for completeness)

**Lakekeeper:** `roles_claim` (dotted path) → `TokenRoles{project, RoleIdent(provider_id, source_id)}`, which requires a project
header (`authn.rs:130-134,391-394,607-651`; `limes/jwks.rs:451-505`).

**rask:** team/role administration is WONTFIX until IdP sync (`docs/DECISIONS.md:412-420`). No action; recorded so that a later
IdP-to-FGA sync keys roles as `(idp_id, role)`, the same idp-id namespace as T3. **Rows:** none.

---

## 3. Order of work (each step RED-first; one row, one commit, one deploy)

1. **T5 boot-fatal + T6 audit fixes.** Hours, no ruling needed. `warm` raises for required providers on `fatal=True` services; the
   catalog's `authenticate` stops auditing an outage as `invalid_token`; refusal records carry the claimed identity. This makes M6
   diagnosable before anything moves.
2. **T3 Principal type + FGA encoding + the provider map (T2/T4).** The Dex providers only, which is D5 / LH-063: re-key and reseed,
   plus a connector-rename test. It needs the owner's one-line answer on the K8s claim (username-encoded vs UID).
3. **T1 per-service ServiceAccounts + projected tokens + a `kubernetes` provider in catalog and lineage** (P8.6), behind P5.3(a).
   The anonymous half is measured: 401. Move the seven caller sites, then delete the service door, the allowlists and the header
   strip. That closes LH-079 (with T8) and CTL-021, and unblocks OpenFGA authn (P8.7) and the workload credential door (P8.8/P8.9 →
   LH-129, CP-001, CP-007).
4. **T7 BFF fallback removal**, a new row.

## 4. Not checked / bounded

- **Lakekeeper:**
  - `crates/lakekeeper/src/api/` files other than `router.rs`, `maintenance.rs`, `mod.rs`, `iceberg/v1/oauth.rs`,
    `management/v1/user.rs` and `management/v1/server.rs`. The rest are handlers, not middleware.
  - Authz internals beyond `instance_admin.rs:137-161` and `authz/mod.rs:1589-1593`.
  - `limes/jwks.rs` beyond `:20-70,245-540`; `request_metadata.rs` beyond `:40-640`; `authentication.md`'s vendor setup sections
    were read, but a few long lines were truncated at 600 characters.
- **rask:**
  - `services/lineage/src/lineage/api/security.py` was not read in full; it shares `service_principal`.
  - The seven caller files are cited by grep line only.
  - `chart/templates/services.yaml` service-subject blocks were not read in full.
- **Not measured:**
  - whether a pod can reach `10.16.51.53:6443` (no exec allowed);
  - whether daprd service invocation forwards `Authorization`;
  - whether Dex access tokens carry `federated_claims`;
  - whether OpenFGA or MinIO can send a bearer when fetching JWKS;
  - the cause of M6's 819 refusals.
- `DynamicContextProvider` was read from the installed source, not exercised.
