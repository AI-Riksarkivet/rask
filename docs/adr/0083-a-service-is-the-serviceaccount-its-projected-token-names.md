# 0083. A service is the ServiceAccount its projected token names (LH-220, D1, 2026-10-02)

Before this row a service authenticated with the estate-wide `APP_API_TOKEN` plus an `x-lance-service-identity`
header it chose itself. Privileged subjects presented a static per-identity `service-token-<id>`, and to check
them the catalog read every peer's key. Measured live on helm rev 261: the shared token plus
`x-lance-service-identity: notifications` read lineage's `/runs` as notifications.

Under D1 each caller presents its pod's projected ServiceAccount token, one per door audience (`rask-catalog`,
`rask-lineage`, `rask-medallion`, 600 s, re-read on every request because the kubelet rotates it at about 515 s).
Each door verifies it offline against the cluster issuer and maps the token's full username,
`system:serviceaccount:<ns>:<sa>`, through an exact map rendered from the chart's identities. The map is what
binds a token to one subject: signature and audience alone accept every account in the cluster that carries
the audience (P5.3 c0). k3s serves discovery and the key set only to a bearer from its private CA, so each door
fetches with an API-audience projected token and `kube-root-ca.crt`. A service is authorized by FGA as its
subject, like a person. `dapr-api-token` stays only on the sidecar-delivered routes, where it proves arrival
and names nobody. The header, the shared-token door and the privileged lists are gone.

Accepted with it: offline verification accepts a deleted pod's token for up to 660 s, because TokenReview would
need auth-delegator on every door, which XC-076 withholds. One pod is one identity, so a job on the shared Ray
head reports as the head's account, `service-trainer` (LH-351). The door-only credentials the old door used are
still minted and stored (LH-350). LH-064 deleted the whole service-token-* family at helm rev 265.

Read back live on helm rev 262: the claimed name answers 401. The producer's own token is admitted at the catalog,
while an unlisted account and a wrong-audience token are refused. The catalog may read only its own signing key.
A service token cannot terminate another tenant's stage. The cascade ran to gold with every hop authenticated by
token.
