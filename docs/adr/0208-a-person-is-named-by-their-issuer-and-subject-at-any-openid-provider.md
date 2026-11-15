# 0208. A person is named by their issuer and subject at any OpenID provider (2026-11-15)

**Status:** accepted. Source: the owner, 2026-11-14 and 2026-11-15, through the multi-question tool on XC-214 (#829):
"Keycloak or dex? Oidc should not matter", then "Any OIDC provider". Amends D5 of
[ADR 0159](0159-four-owner-rulings-recorded-from-agent-memory.md), whose "never the identity provider's raw `sub`" held
only for Dex; [ADR 0181](0181-a-dex-client-signed-in-for-itself-is-a-machine-principal.md)'s machine principal stands, told
apart by the rule below.

## Context

Every governed door named a person only from Dex's `federated_claims` (the connector id and the upstream user id, which
Dex adds when a client asks for the `federated:id` scope), mapped through `RASK_OIDC_CONNECTORS`. A token from any other
OpenID provider carries no such claim and was refused 401, and the chart always added `federated:id` to the console's
scopes. So the platform's identity provider had to be Dex, although the IdP is a platform service the lakehouse names
and never bundles (the cloud-native fact). A client signed in for itself was told only by Dex's `sub` encoding.

The reference answers. OpenID Connect Core 1.0 § 2 makes `sub` "a locally unique and never reassigned identifier within
the Issuer", and the issuer's signature binds `iss` and `sub`. RFC 9068 § 2.2 gives a client-credentials access token a
`sub` that identifies the client and a required `client_id`, and § 5 puts keeping a client's `sub` apart from every
resource owner's on the authorization server. Lakekeeper (`docs/docs/authentication.md` on `main`, read 2026-11-15) names
a user `oidc~<ID from the provider>`, reads `sub` unless `LAKEKEEPER__OPENID_SUBJECT_CLAIM` names other claims (the first
present wins), and reads no broker's private claims. `lance_docs/` does not cover identity. Polaris and Gravitino were
not read.

## Decision

1. **A person is `<idp id>~<subject>`, from what every OpenID provider issues.** The idp id is the one the deployment
   names for its issuer (`RASK_OIDC_IDP_ID`, chart `auth.oidc.idpId`); the subject is the first of
   `RASK_OIDC_SUBJECT_CLAIMS` (chart `auth.oidc.subjectClaims`) the token carries, `sub` when none is named. A deployment
   names another claim first where its provider's `sub` is not the stable id a grant needs (Entra's `oid`).
   `lakehouse.governed.idp_identity.SubjectNaming`.
2. **Dex's connector map stays, as the one alternative rule.** A deployment whose issuer is a Dex brokering several
   upstream providers may name each connector's idp id instead (`RASK_OIDC_CONNECTORS`, chart `auth.oidc.connectors`),
   so each connector's people keep apart and a renamed connector re-keys nobody (ADR 0159 D5, [[LH-063]]).
   `ConnectorNaming`. Exactly one rule names a person: the service refuses to boot, and the chart to render, with
   neither or both, and with subject claims beside a connector map, which never reads them.
3. **The console asks for `federated:id` only under the connector map.** The chart adds the audience scope always and
   that scope only when it renders a connector map.
4. **A client signed in for itself is the machine `dex-client~<client id>` under either rule** when its token's `sub` is
   the client's: equal to the `client_id` it names (RFC 9068 § 2.2), or Dex's encoding of the client its `azp` names.
   `azp` alone is never compared with a raw `sub`: every id token names its client there, so a person whose provider
   let them pick a `sub` equal to a public client's id would read as that client. The
   idp id keeps its ADR 0181 spelling; no live grant is re-keyed.
5. **The development estate keeps its principals.** With `dex.enabled` and no `auth.oidc.idpId`, the chart maps the dev
   Dex's `local` connector to `dex`, as before, so every principal its grants name (`dex~<user id>`) is unchanged. Dex
   stays the development fixture; no fixture is swapped.
6. **Token exchange is out of scope.** RFC 8693 on-behalf-of is the identity provider's and the calling client's; a
   token exchanged for the lakehouse's audience is a token like any other at the door, which still refuses one issued
   for another audience.

## Consequences

- **One person issuer per deployment.** `RASK_OIDC_ISSUER` names one issuer and the rule names its people. A second
  person issuer beside it (Lakekeeper's several providers) is not built: each would need its own idp id, discovery
  override and egress rule in the chart, and no row asks for it.
- **A provider whose client-credentials `sub` is not the client's** (Keycloak's service-account user id) names that
  client a person under rule 1. Such a client signs in as a person with its own tuples, or the provider is configured to
  issue RFC 9068 tokens. The other way round, a person whose `sub` equals a `client_id` their token names would be
  read as that client; RFC 9068 § 5 makes keeping the two apart the authorization server's, and the client
  check runs first under both rules so one token is never read both ways.
- Four facts. GitOps: the idp id, the subject claims and the connector map are chart values. Cloud native: the IdP is the
  platform's, any vendor. Lance-idiomatic: not touched. Zero trust: the issuer's signature binds `iss` and `sub`, the
  audience check stays, and a token from an issuer the deployment does not name is refused 401.
