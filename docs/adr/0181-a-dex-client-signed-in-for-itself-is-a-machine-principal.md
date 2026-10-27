# 0181. A Dex client signed in for itself is a machine principal (2026-10-27)

**Status:** accepted. Source: XC-174 (#681), admitted by the owner on 2026-10-20 ("a Dex client-credentials token is a
machine principal named `<idp-id>~<client-id>`, as LH-063 names a person, admitted at the verifier"). Extends ADR 0156 C5
and ADR 0159 D5.

## Context

A machine reached the governed doors only as a Kubernetes service account (`kubernetes~system:serviceaccount:<ns>:<sa>`,
ADR 0156 C5). A pipeline running outside Kubernetes had no identity of its own: the Dex verifier admitted a token only
when it named the connector a person signed in through (`federated_claims`, ADR 0159 D5), so such a pipeline signed in
as a person, with a password, and its writes were attributed to that person.

Dex serves the OAuth 2.0 client-credentials grant from v2.46.0, when `oauth2.grantTypes` lists it. Its token, read from
dex v2.46.1 (`server/grants/clientcredentials.go`, `server/tokens/issuer.go`, `server/tokens/claims.go`) and measured on
a real v2.46.1 with the dev fixture's configuration on 2026-10-27:

- carries the claims of the client itself, `UserID = client.ID`, under the empty connector id, which no configured
  connector may carry, so its `sub` is `IDTokenSubject{user_id = <client id>}` (`Cg1yYXNrLXBpcGVsaW5l` for
  `rask-pipeline`);
- names the client in `azp` whenever it is issued for another audience, which every door's audience is
  (`aud: [rask-lakehouse, rask-pipeline]`, `azp: rask-pipeline`);
- carries no `federated_claims`: the grant refuses `federated:id` (`invalid_scope`), and a public client is refused the
  grant (`unauthorized_client`).

## Decision

**A client-credentials token is the machine `dex-client~<client id>`.** The IdP verifier (`IdpVerifier`, in
`service_kit.governed.person_identity`) admits a token carrying no federated claims when its `sub` is exactly
`IDTokenSubject{user_id = azp}`. A person's `sub` always encodes a connector and the issuer's signature binds both
claims, so no person's token, with or without `azp`, passes for a client. Any other token without federated claims is
refused 401 as before.

**`dex-client` is a fixed idp id, like `kubernetes`.** Both are machine idp ids (`principal.MACHINE_IDPS`), so whether a
principal is a machine is answered by its key alone (`Principal.is_machine`), wherever that question is asked: who is
addressed as a person (`is_person_subject`), the kind an offboarding records, and whom a create seeds as owner. No Dex
connector may be mapped to either (`RASK_OIDC_CONNECTORS`), so a client id and a person's user id never name one
principal. A configured idp id per deployment was not taken: the machine test reads a key with no settings at hand, and
a configured name would have to reach every reader of a key.

**A Dex client is a `ServicePrincipal`.** The estate's one test of a machine caller is that type, so a client is never
seeded owner of what it creates (LH-433), names the person who administers a project it creates, and is never addressed
as a person. `Principal.is_service` stays the test of a Kubernetes account alone: it names the account's own event-signing
key, which a Dex client does not hold.

**Any client authenticates; OpenFGA alone authorizes it.** As with service accounts, no list of clients stands in front
of the door: a client the IdP issues a token for, asking for the lakehouse's audience, which the audience client issues
only to a client it trusts, is authenticated, and what it may do is decided by the tuples naming `dex-client~<client id>`.

**Every confidential client the audience trusts can sign in for itself.** Dex v2.46.1 lists grants for the whole issuer
(`oauth2.grantTypes`) and has no per-client grant setting (`storage.Client` carries none), so the platform keeps the
audience client's `trustedPeers` to the clients meant to reach the lakehouse. A public client is refused the grant, and
a client signing in for itself without asking for the lakehouse's audience is issued a token naming no `azp`, which the
verifier refuses. In the dev fixture that leaves `rask-pipeline` alone: `rask-web` is public, and `rask-lakehouse`'s own
token names no `azp`.

**The platform's Dex declares the clients.** Production's Dex is the platform's (owner, 2026-10-04): it must be v2.46.0
or later with `client_credentials` in `oauth2.grantTypes`, each pipeline a confidential client the audience client lists
in `trustedPeers`. The dev fixture (`deploy/fixtures/dex`) runs v2.46.1, lists the grants Dex served before and
`client_credentials`, and declares one machine client, `rask-pipeline`, in `machineClients`.

## Consequences

- A pipeline off the cluster writes as itself: the live leg
  `tests/e2e-py/test_a_pipeline_outside_kubernetes_writes_as_its_own_dex_client.py` signs in as `rask-pipeline`, writes
  a table on a writer grant, finds `rask.author` naming `dex-client~rask-pipeline` on the commit, and is refused the drop
  an owner could make.
- `ServicePrincipal` carries a machine `Principal`; `ServicePrincipal.for_account` builds a service account's.
- The dev fixture's machine-client secret is a literal in its values, as its other clients' are, rendered into Dex's
  configuration file (never an environment variable); a production client's secret is the platform's, in its secret
  store.
- The governed Dagger lane and the compose side stacks still run Dex v2.42.0, which serves no client-credentials grant;
  neither runs the live leg.
- A Dex client's token lives as long as the platform's Dex issues it (the dev fixture's default, 24 h); a pipeline
  fetches a fresh one per run.
