# 0170. The catalog's store identity is its ServiceAccount: its own IO and every vend are sessions its token is exchanged for (2026-10-18)

**Status:** accepted. Amends [ADR 0166](0166-a-secret-read-from-the-store-is-re-read-on-a-ttl.md) (the catalog holds no
key pair in the secret store; lineage and maintenance still do) and [ADR 0168](0168-the-development-object-store-is-rustfs.md)
§ Decision 2 (the development store trusts the cluster's ServiceAccount issuer). Source: XC-083 (#204), started by the
owner with `/implement #204`, and two owner rulings of 2026-10-18 through the multi-question tool: the provisioning hook
prunes every store user and rask policy git does not declare, and the per-base credential that stays a static pair is
parked as its own row (XC-168, #662). Evidence: `docs/audits/2026-10-18/rustfs_web_identity_measure.py`, run against a
throwaway RustFS 1.0.3 with a stand-in issuer, 20 of 20 checks as rask needs them (`rustfs-zero-trust.md` § "Measured:
web identity").

## Context

The catalog held a static scoped pair, the store user `rask-catalog`, read from the secret store as `catalog-s3`
(ADR 0166). It signed the catalog's own IO, and it signed the STS `AssumeRole` every vend makes, so it was the parent of
every credential a client was handed. A static key does not expire on its own, and whoever reads it holds the whole
data plane until someone rotates it; the rotation itself was a second user under the same policy, because RustFS
refuses `AssumeRole` from an access key (`rustfs@1.0.3:rustfs/src/admin/handlers/sts.rs:219-221`). The other vending
plug exchanged the CALLER's token: it could not serve a service principal, it needed the store to trust the people's
IdP, and on RustFS it handed every holder of a token for that audience the provider's whole role policy.

RustFS 1.0.3 serves `AssumeRoleWithWebIdentity` (`sts.rs:186-195,399-441`), and its source answers each question the
catalog's own token raises:

- A discovery document with no `authorization_endpoint` is a workload provider when
  `RUSTFS_IDENTITY_OPENID_HIDE_FROM_UI` is on (`rustfs@1.0.3:crates/iam/src/oidc/provider.rs:128-153`).
- With an explicit `RUSTFS_IDENTITY_OPENID_ISSUER`, discovery is fetched from `RUSTFS_IDENTITY_OPENID_CONFIG_URL` and the
  discovered `issuer` must equal the explicit one (`provider.rs:498-588`): the shape rask's issuer mirror serves, the
  cluster issuer's document with `jwks_uri` pointed at the mirror (`service_kit.governed.issuer_mirror`). k3s answers
  discovery only to a bearer, so the mirror is what the store can read.
- A token is checked for signature, issuer, audience and expiry, with no nonce, and the key set is fetched again once
  on a failure (`rustfs@1.0.3:crates/iam/src/oidc/runtime.rs:740-798`).
- With `RUSTFS_IDENTITY_OPENID_CLAIM_NAME=sub` and no role policy, a verified token is granted the canned policy its
  `sub` names (`rustfs@1.0.3:crates/iam/src/federation/mapper.rs:257-298,373-382`), a name that may hold `:`
  (`rustfs@1.0.3:crates/iam/src/sys.rs:1888-1897`), and a `sub` that names no policy is refused
  (`rustfs@1.0.3:rustfs/src/admin/service/federated_identity.rs:322-440`).
- A session's request is allowed only where its session policy and the policy its claims name both allow it, the STS
  path an `AssumeRole` session takes (`sys.rs:1532-1665`).
- The provider's private origin must be the operator's one outbound allowance, `RUSTFS_OUTBOUND_ALLOW_ORIGINS`
  (`rustfs@1.0.3:crates/utils/src/egress.rs:25-29`), and a provider whose discovery fails at boot is recovered in the
  background (`rustfs@1.0.3:rustfs/src/startup_auth.rs:58-66`; `runtime.rs:151-216`).

Measured on the throwaway store: the catalog-subject token is issued a session that reaches what its canned policy names
and is refused a bucket it does not; a vend-shaped read session's GET in scope answers 200 and another prefix, another
tenant and a PUT 403; a session policy naming a bucket the canned policy lacks grants nothing; Lance creates and appends
through a write session; and another ServiceAccount's token, a same-named account in another namespace, another
audience, an expired token, a forged one and another issuer's are each refused.

## Decision

1. **The catalog's identity at the store is its ServiceAccount.** Its pod projects a token for
   `objectStore.identity.audience` (default `rask-object-store`) at `/var/run/secrets/rask/identity/<audience>/token`
   (`LANCE_S3_WEB_IDENTITY_TOKEN_FILE`), read again at every exchange because the kubelet replaces it. Unsigned STS
   `AssumeRoleWithWebIdentity` (`storage.sts_client`) exchanges it for:
   - **the catalog's own IO**: a session of `objectStore.identity.sessionSeconds` (3600 s) named `catalog`, held in
     `service_kit.governed.store_session.SESSIONS` and exchanged again by `StoreSession`'s background loop once half its
     life is used. A request never calls STS: it takes the held session, and one within 60 s of its expiry is refused
     503 (code 17) rather than handed out. The boot exchange waits a minute for a store, its provider and the hook that
     writes the policy, then fails the boot closed.
   - **each vend**: a session of `vending.ttlSeconds` (900 s) narrowed by the per-table, per-tier session policy
     `vending.build_session_policy` writes, named for the caller's verified subject in AWS's character set
     (`vending.role_session_name`). The caller's own token never leaves the catalog.
2. **The development store grants a token the policy its subject names.** The rustfs fixture's `identityProvider` sets
   the discovery URL (rask's issuer mirror), the cluster's issuer, the audience as client id, `CLAIM_NAME=sub`,
   `HIDE_FROM_UI`, the mirror's origin as the outbound allowance, and no role policy. The scoped-users hook writes the
   catalog's canned policy under its account's username, `system:serviceaccount:<ns>:<release>-sa-catalog`
   (`lance.saAccount`), and no user holds it. A platform store's owner configures it to trust the cluster's issuer for
   the audience and to grant the catalog's subject its policy; on AWS that is an IAM OIDC provider and the role
   `vending.assumeRoleArn` names, whose trust policy names the subject.
3. **The store's identities are what git declares.** The hook's install run and its post-upgrade run
   (`…-object-store-scoped-users-prune-<rev>`) pass `--prune`: every regular user the run does not declare is removed,
   then every canned policy rask owns (`rask-*`, or named after a ServiceAccount) that it does not create
   (`scripts/rustfs_admin.py`); a policy rask does not own is never touched. The pre-upgrade run provisions what the new
   pods need and prunes nothing: the previous release's pods present a retired user until the upgrade has replaced
   them. The next pruning run on the dev estate removes `rask-catalog`.
4. **One plug vends a credential, and it holds no key.** `vending.mode` is `web_identity` (the default) or `mode_b`.
   The `sts` plug, `AssumeRole` signed with a key pair, and the exchange of the caller's token are removed: every store
   rask targets, AWS STS, Ceph RGW and RustFS, serves `AssumeRoleWithWebIdentity`. The catalog's settings refuse a
   catalog reading its secrets from the store without its token, a key pair beside the token, and a token with no named
   STS endpoint, which would reach the public AWS STS (`Settings._validate_store_identity`). The environment pair is
   the offline and Dagger lanes' alone, with the secret store off.
5. **The issuer mirror serves the store as well as OpenFGA.** It renders from `chart/templates/issuer-mirror.yaml`
   whenever `auth.issuerMirror.enabled` and either the bundled OpenFGA or the development store is in the release.

## Considered options

- **`AssumeRole` signed by a user rotated through the store** (ADR 0166's model). Rejected: a scoped static key is not
  a fix; it outlives every session it signs, and the rotation it needs is the problem, not the remedy.
- **A role policy on the provider**, RustFS's documented shape. Rejected: every token the cluster's issuer signs for the
  audience would hold the catalog's data plane, and any pod may project a token for any audience as its own
  ServiceAccount (`mapper.rs:257-298`).
- **An STS policy mapping on the parent RustFS derives from `sub` and `iss`** (`mapper.rs:112-129`), which the binding
  reads first. Rejected: only the `import-iam` admin route writes one (`rustfs@1.0.3:rustfs/src/admin/handlers/user.rs:1256-1290`),
  a zip archive the hook would have to build, keyed by a hash nothing on the policy list names.
- **The caller's own token as the web identity.** Rejected: it cannot serve a service principal, and the store would
  trust the people's IdP with the role-policy hazard above.
- **The provider's settings in a ConfigMap rask renders and the fixture reads.** Rejected: a change would reach the store
  only on a restart nothing triggers, where a value in the fixture's pod template rolls it. The two charts' halves are
  held equal by `tests/unit/test_a_provisioned_identity_is_one_the_service_uses.py`.
- **Pruning in every run.** Rejected: a pre-upgrade prune cuts off the previous release's pods while they still serve.

## Consequences

- **The catalog holds no store key of its own** on any estate the chart renders: no pair in its environment, none in
  the secret store (`tests/unit/test_every_secret_in_the_dev_store_has_exactly_its_readers.py`), and no store user. The
  live estate is the row's closing step: deploy, then read back that the catalog signs with sessions, that
  `rask-catalog` is gone, and that another account's token is refused.
- **One static pair remains**: a data base read under its own credential reference ([[LH-067]], [[LH-273]],
  `catalog.multibase.baseCredentialRefs`), on the dev estate `lh067-second-store`. Parked by the owner as XC-168 (#662).
- **STS rides the store's own endpoint.** `LANCE_S3_STS_ENDPOINT` is the store's address, where RustFS and Ceph serve it;
  an AWS deployment needs its regional STS endpoint, for which the chart has no value yet.
- **The store's STS is on the catalog's path.** An STS outage shorter than half a session (30 minutes) serves nobody a
  503; a longer one refuses the catalog's object-store calls once the held session expires, and every vend while it lasts.
- **A refused token costs a round trip to the issuer.** RustFS fetches discovery and the key set again for each token
  that fails verification (`runtime.rs:740-798`; the stand-in issuer logged one of each per refusal), which on the estate
  is one request to the mirror and one from it to the API server.
- **RustFS ignores `RoleArn` and `RoleSessionName`.** The session name is for AWS's CloudTrail, where a vended session
  reads as the person or service it was vended for.
- **The dev OpenBao keeps a stale `secret/catalog-s3` until its pod is replaced.** The seed writes what it declares and
  deletes nothing; the catalog's role no longer reads the secret, and the user it names is removed. A fresh install from
  git holds none.
- **The store holds one temporary user per session**, as for every vend: the catalog's own IO adds one per half hour.
