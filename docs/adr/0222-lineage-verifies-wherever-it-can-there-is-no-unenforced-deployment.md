# 0222. Lineage verifies wherever it can: a deployment has no unenforced state (2026-11-20)

**Status:** accepted. Source: XC-219 (#843), finding 9 #1 of the 2026-11-18 whole-lakehouse audit, decided by the owner
on 2026-11-20: "Remove the unenforced state".

## Context

The chart carried `signing.enforce`, lineage's switch. Off, lineage listed no signer and verified nothing, while every
service went on signing. The audit found what that state admits: lineage's bus and outbox doors recorded a
`rask_signature` an event merely carried, the event took the catalog's own feed row (`provenance.feed_key` keeps the
bare key for a signed change), and the projection served the facet as the signature lineage verified.

Stripping an unverified facet at those doors, as the HTTP door does, was considered first and measured: the catalog's
commit reconciler counts a commit as announced only by a feed row its own signature is on
(`catalog.services.commit_reconcile.announced`, [[LH-576]]), so with the facet stripped it would announce every commit
again on every tick and never converge. Teaching the reconciler to accept unsigned rows in that state would reopen the
gap LH-576 closed, and make two services agree on one switch.

The references settle nothing here. Lance and lance-namespace define no events and no event signature
(`lance_docs/`, both trees). The catalog the research reads signs no event and trusts its bus connection's
credentials, with no verifier of its own; rask has that control too (the bus admits each service to its own subjects)
and signs on top of it.

## Decision

- The chart has no `signing.enforce`. Wherever `auth.enabled` is on, lineage carries `LINEAGE_SIGNERS` and
  `LINEAGE_DELEGATORS` and reads every signer's published keys; a render with auth on and no signer is refused.
- Lineage refuses to start when authentication and a secret store are on and no signer is listed
  (`Signing.require_signers_where_they_can_be_verified`).
- A process with no secret store has no key to verify against, and one with authentication off authenticates nobody:
  the offline and Dagger lanes run so, unchanged, and admit nothing on a present facet.

## Why

- **Zero trust.** No deployed state trusts a signature by its presence.
- **GitOps.** A deployment's signing posture is one rendered state, not a value two services must read alike.

## Consequences

An overlay that still sets `signing.enforce` is refused at render: `chart/values.schema.json` admits no key under
`signing` that `values.yaml` does not declare (measured 2026-11-20: `additional properties 'enforce' not allowed`). The bus doors keep their own mode, `signing.doors`. Pinned by
`tests/chart/test_event_signing_is_wired_by_the_chart_in_every_deploy_mode.py` and
`services/lineage/tests/test_lineage_refuses_an_event_no_listed_signer_signed.py`.
