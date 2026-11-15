# 0208. A person is named by their issuer and subject at any OpenID provider (2026-11-15)

**Status:** accepted. Source: the owner, 2026-11-14 and 2026-11-15, through the multi-question tool on XC-214 (#829):
"Keycloak or dex? Oidc should not matter", then "Any OIDC provider"; and, after #829 closed on helm rev 16 with three
limits stated, "Keep fix undone": several issuers, a client namespace that names no vendor, and Keycloak's client
tokens, decided here the same day. Amends D5 of [ADR 0159](0159-four-owner-rulings-recorded-from-agent-memory.md), whose
"never the identity provider's raw `sub`" held only for Dex, and the idp id of
[ADR 0181](0181-a-dex-client-signed-in-for-itself-is-a-machine-principal.md)'s machine principal.

## Context

Every governed door named a person only from Dex's `federated_claims` (the connector id and the upstream user id, which
Dex adds when a client asks for the `federated:id` scope), mapped through `RASK_OIDC_CONNECTORS`. A token from any other
OpenID provider carries no such claim and was refused 401, and the chart always added `federated:id` to the console's
scopes. So the platform's identity provider had to be Dex, although the IdP is a platform service the lakehouse names
and never bundles (the cloud-native fact). A client signed in for itself was told only by Dex's `sub` encoding and named
under the fixed idp id `dex-client`, whatever issued it.

The reference answers. OpenID Connect Core 1.0 § 2 makes `sub` "a locally unique and never reassigned identifier within
the Issuer", and the issuer's signature binds `iss` and `sub`. RFC 9068 § 2.2 gives a client-credentials access token a
`sub` that identifies the client and a required `client_id`, and § 5 puts keeping a client's `sub` apart from every
resource owner's on the authorization server. Keycloak does not follow it: a client-credentials token's `sub` is the id
of the client's hidden service-account user, with the client in `azp` and a `client_id` claim (Keycloak user list
2017-12, "JWT 'sub' claim in client credentials flow"; a third-party service-account guide names the `client_id` claim).
Entra marks an app-only access token with the optional claim `idtyp: app`, "the most accurate way for an API to
determine if a token is an app token or an app+user token" (Microsoft's optional claims reference). Lakekeeper
(`docs/docs/authentication.md` on `main`, read 2026-11-15) keeps one primary provider and adds further ones under
`LAKEKEEPER__OPENID_PROVIDERS__<IDP_ID>__`, names each provider's users `{idp_id}~{subject}`, reads `sub` unless subject
claims are named (the first present wins), and tells an Entra application by `idtyp: app`; it names no Keycloak claim.
`lance_docs/` does not cover identity. Polaris and Gravitino were not read.

## Decision

1. **A person is `<idp id>~<subject>`, from what every OpenID provider issues.** The idp id is the one the deployment
   names for the issuer (`RASK_OIDC_IDP_ID`, chart `auth.oidc.idpId`, required; `dex` with the development Dex); the
   subject is the first of the issuer's subject claims (`RASK_OIDC_SUBJECT_CLAIMS`, chart `auth.oidc.subjectClaims`) the
   token carries, `sub` when none is named. `lakehouse.governed.idp_identity.SubjectNaming`.
2. **Dex's connector map stays as the one alternative rule for an issuer.** A Dex brokering several upstream providers
   may name each connector's idp id instead (`RASK_OIDC_CONNECTORS`, chart `auth.oidc.connectors`), so each connector's
   people keep apart and a renamed connector re-keys nobody (ADR 0159 D5, [[LH-063]]). `ConnectorNaming`. Subject claims
   are refused beside a connector map, which never reads them. The idp id names no rule: a connector map, or else the
   subject claims, does, so naming the idp id explicitly re-keys nobody.
3. **Several issuers.** Beside the console's issuer (`RASK_OIDC_ISSUER`), `RASK_OIDC_PROVIDERS` (chart
   `auth.oidc.providers`) names further issuers, each with its own idp id, subject claims, connector map, client marker
   and split-horizon discovery URL, as Lakekeeper adds providers beside its primary. A token is named by the rule of the
   issuer its verified `iss` is. An issuer named twice, or an idp id two issuers name, is refused at boot: two issuers'
   people never share a namespace.
4. **A client signed in for itself is `<idp id>-client~<client id>`, under its own issuer's idp id**, so one client id
   at two issuers is two machines. Every idp id ending in `-client`, and `kubernetes`, names machines, so no person's idp
   id may. The development Dex's idp id is `dex`, so its clients keep the principal `dex-client~<client id>`.
5. **How a token is told as a client's is the issuer's.** Where the deployment names the issuer's client marker
   (`RASK_OIDC_CLIENT_MARKER`, a provider's `client_marker`, chart `clientMarker`: `{claim, value}`, the value any when
   unset; `{claim: client_id}` for Keycloak, `{claim: idtyp, value: app}` for Entra), a token carrying it (not null) is a
   client's, named by its `client_id`, `azp` or `appid`, and one naming none is refused 401. A marker on a claim every
   verified token carries (`iss`, `sub`, `aud`, `exp`, `iat`) marks nothing and is refused at render and at boot. Otherwise a token is a client's when its
   `sub` is the `client_id` it names (RFC 9068 § 2.2), or Dex's encoding of the client its `azp` names. `azp` alone is
   never compared with a raw `sub`: every id token names its client there, so a person whose provider let them pick a
   `sub` equal to a public client's id would read as that client.
6. **The console asks for `federated:id` only under the console issuer's connector map.** The chart adds the audience
   scope always and that scope only when it renders a connector map.
7. **The development estate keeps its principals.** With `dex.enabled` the chart names the dev Dex `dex` unless
   `auth.oidc.idpId` names another, and maps its `local` connector to `dex` unless `auth.oidc.connectors` names a `local`
   of its own or `auth.oidc.subjectClaims` reads people by subject, as before, so every person (`dex~<user id>`) and
   client (`dex-client~<client id>`) its grants name is unchanged. Dex stays the development fixture; no fixture is
   swapped.
8. **Token exchange is out of scope.** RFC 8693 on-behalf-of is the identity provider's and the calling client's; a
   token exchanged for the lakehouse's audience is a token like any other at the door, which still refuses one issued
   for another audience.
9. **A further issuer is refused at render as at boot.** The chart refuses an issuer that is not https, one named twice,
   an idp id two issuers name, subject claims beside a connector map, and a marker on a claim every token carries, so a
   bad entry fails the release before any pod starts.

## Consequences

- A further issuer's people are reached through the doors once the deployment's egress admits it: `auth.oidc.egress`
  admits the governed doors to every person issuer under `networkPolicy.enabled`.
- A provider whose client tokens carry no mark and whose client `sub` is not the client's names such a client a person,
  until the deployment names its client marker.
- Entra's `idtyp` is an optional claim the tenant turns on for its access tokens; without it, an Entra app's token
  carries no mark and its app is read as a person.
- Under a client marker, a person's token that carries the marked claim is read as a client's: the marker is chosen per
  issuer from what that issuer puts in its client tokens alone (Keycloak's `client_id` appears in service-account tokens
  only; Entra's `idtyp` is `app` in app-only tokens only).
- Four facts. GitOps: every issuer, idp id, subject claim, connector map and client marker is a chart value. Cloud
  native: the IdPs are the platform's, any vendor. Lance-idiomatic: not touched. Zero trust: the issuer's signature binds
  `iss`, `sub` and the marking claim; the audience check stays; a token from an issuer the deployment does not name is
  refused 401, and an issuer's idp ids are its own.
