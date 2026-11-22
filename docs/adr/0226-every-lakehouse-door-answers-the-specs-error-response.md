# 0226. Every lakehouse door answers the spec's ErrorResponse (2026-11-22)

**Status:** accepted; ends the deferral [ADR 0224](0224-the-library-holds-what-two-services-share.md) § "The answers
group" recorded ("Giving the domain body the spec's `code` changes what a rask-only door answers, and is decided on its
own"). Source: LH-578 (#805), decided by the owner on 2026-11-22: "every failure uses the one format with the Lance
failure number".

## Context

Two exception families answered the lakehouse's failures. The `lance_namespace` hierarchy, through
`lakehouse.problems.ns_errors.install_problem_handlers`, answered RFC 9457 plus the spec's `code` and `error`
(`lance_docs/ns_catalog/spec.yaml` `ErrorResponse`, whose one required field is `code`). A second family,
`lakehouse.problems.domain_errors.DomainError` (a `fastapi.HTTPException` with seven classes in the library and three in
the auth plane), answered `about:blank#<class>` with four keys and no `code`. Measured on `9d8d63a24` by driving both
installers in a scratch app:

- Lineage's doors relayed every catalog answer through the domain family, so a lineage 400, 401, 403, 404, 409 or 503
  carried no `code`, and a 4xx `detail` was the catalog's raw JSON as a string.
- The issuer mirror installed only the domain handler: a refused upstream fetch answered **502** with the issuer's
  in-cluster URL in `detail`, and an unexpected exception Starlette's plain-text 500.
- The domain handler did not redact a 5xx: `ProviderUnavailableError` named the issuer's URL, and
  `IdentityTokenUnavailableError` the token file's path, on any door they reached.
- An extension member could add `code` to a domain body, and one named `instance` passed the spec family's filter
  whenever no span was active.

No catalog door (`/v1/...`, `/management/v1/...`) answered a domain body: its authentication converted the one domain
error that reached it, and its importers were forbidden the module.

## Decision

1. **The `lance_namespace` hierarchy is the one exception family at every door.** `domain_errors.py`,
   `register_handlers` and the import contract that kept the catalog's API off it are deleted, not given a `code`.
   `install_problem_handlers` is the one handler set and `envelope.problem_body` the one body.
2. **A rask error that needs a name of its own subclasses the spec class whose code it answers.**
   `ProviderUnavailableError`, `IdentityTokenUnavailableError` and the mirror's `IssuerUnansweredError` are
   `ServiceUnavailableError`s (503, code 17). The issuer mirror therefore answers **503, not 502**: the spec's error
   table has no 502, and one code rides one status (`ns_errors`' maps are unchanged). The mirror logs the issuer's URL
   and answer at warning before it raises, the operator's copy of what the body no longer says.
3. **Lineage relays the catalog's refusal only where its caller asked the catalog, and only a refusal of the caller's
   name.** A failed call through the catalog raises one class, `CatalogAnsweredError`. For a describe lineage asks under
   the caller's own bearer (`lineage.services.names`, the one place it does), a refusal a describe owes its caller keeps
   the catalog's status, `code` and `detail`: a 4xx whose body is a problem body carrying invalid input, a concurrent
   modification or an invalid table state (codes 13, 14, 19) at the status the spec maps that code to. A not-found, a
   denial or an unauthenticated answer (codes 1, 4, 15, 16) resolves the name to no object instead. Everything else is
   503, code 17 and "the catalog is unavailable", the head of the answer in the log at warning: a 5xx, a 429, a body
   that is no problem body, code 0 (the catalog serves no such operation, which is lineage's address for it or its
   request being wrong), a status the code does not map to, and every answer to a call lineage makes as itself
   (`catalog_reader.translate_catalog_errors`), because what the catalog refuses lineage's own identity (its grant, its
   token, its rows, its provenance tables) is nothing lineage's caller asked for or may read. So lineage answers the
   catalog's own status and code or the outage, never a status of its own for the catalog's code.
   It is one class and not a `PermissionDeniedError`: lineage's lanes park a `PermissionDeniedError` as lineage's own
   refusal of an event, while a catalog 403 to lineage's own identity is an outage of its grant there, which is asked
   for again.
4. **One redaction rule.** Every 5xx `detail` is `Internal Server Error`, whatever class raised it; the message reaches
   the log only.
5. **One carrier for extension members.** `ns_errors.ProblemExtensions` holds them (`PartiallyAppliedError`,
   `SessionPolicyTooLongError`), and the envelope leaves out any that names a member it owns, `instance` included whether
   or not a span put one there. Every exception class is named `…Error`.
6. **`Retry-After` rides built responses only.** No raised error carries a header; a refusal that says when to come back
   (maintenance mode, the signing gate, the write shed) is built, through `envelope.problem_response` or
   `envelope.send_problem` for a pure-ASGI layer.
7. **The published documents say what every door answers.** Every operation of `docs/catalog-openapi.json` and
   `docs/lineage-openapi.json` declares a `default` naming `ProblemDetails`; a rask-only operation's 422 names it too
   (with its `errors`), where FastAPI's `HTTPValidationError` named a body no door served; `ProblemDetails` declares the
   optional `instance`.
8. **The probes are not error bodies.** `/readyz` answers its `Readiness` model at 503 and `/livez` answers
   `Liveness(status="stalled")` at 503 (it answered `{}`), both declared: a probe's body is the process's answer, read by
   the kubelet by its status, not an error a client dispatches on (ADR 0224 § "The answers group": "`schemas.health`
   stays out").

## Consequences

- Every lineage error body carries `code` and `error`, and `instance` while a span is active. One answering a failed
  call through the catalog has the `type` `https://lance.org/problems/cataloganswerederror`, and a relayed 4xx `detail`
  is the catalog's sentence.
- A refusal the catalog gives lineage's own identity answers lineage's caller 503, code 17, where it answered the
  catalog's 4xx with the catalog's body: a producer whose event lineage took retries it, and the catalog's sentence
  about lineage's grant or tables stays in lineage's log.
- A lineage 503 on an unreadable catalog token carries code 17 and no longer names the token's path.
- The issuer mirror's `/readyz` names its issuer `unavailable`, as every process names a dependency, where it carried
  the failed fetch's message with the issuer's address; the fetch logs both.
- OpenFGA reads the mirror's failed fetch as 503 where it read 502, and treats the two alike (read 2026-11-22 at
  v1.18.3): `GetConfiguration` refuses any status but 200 (`internal/authn/oidc/oidc.go:225-226`), and `GetKeys` fetches
  the key set through keyfunc v2.1.0, whose default extractor does the same (`options.go:122-123`).
- The title and `type` of two catalog 5xx bodies follow their classes' new names: `PartiallyAppliedError` (503, code 17)
  and `SessionPolicyTooLongError` (500, code 18). No catalog door changes a status or a `code`.
- The console reads only `status` and `detail`, so it needs no change; a 5xx it shows is the redacted sentence. Its
  generated types lose `HTTPValidationError` and gain `ProblemDetails` on every operation.
