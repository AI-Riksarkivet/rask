package main

import (
	"context"

	"dagger/rask/internal/dagger"
)

// A LANE'S CATALOG HOLDS NO STORE KEY, as the chart's does not ([[XC-081]], ADR 0170): it exchanges a ServiceAccount
// token at the throwaway store's STS for a session under the canned policy the chart grants it. A lane has no Kubernetes
// to project that token, so `scripts/lane_store_issuer.py` stands in for the cluster's issuer: one key per run, served
// as discovery and a key set the store trusts as its workload provider, and a token minted with it in the claims k3s
// projects. The store's root pair stays the store's, and its provisioning step's, and reaches no catalog.

// laneIssuerURL is where the store reaches the stand-in issuer, a service it binds as `issuer`. RustFS refuses a private
// address unless its exact origin is allowed (`RUSTFS_OUTBOUND_ALLOW_ORIGINS`), and every address in a lane is private.
const laneIssuerURL = "http://issuer:8080"

// laneIssuer is the `iss` the stand-in issuer publishes and signs with, which the store must name as its provider's issuer:
// the cluster's, as a projected ServiceAccount token carries it.
const laneIssuer = "https://kubernetes.default.svc.cluster.local"

// laneCatalogSubject is the subject the chart's catalog ServiceAccount carries in release `rask`, namespace `default`, and
// the name of the canned policy the store grants it.
const laneCatalogSubject = "system:serviceaccount:default:rask-sa-catalog"

// laneStoreAudience is the audience the chart projects a workload's store token for (`objectStore.identity.audience`).
const laneStoreAudience = "rask-object-store"

// laneTokenSeconds outlives every lane's run: the catalog re-exchanges the same file at half its session's life, and a
// token that expired first would refuse that renewal, where the kubelet would have rotated a projected one.
const laneTokenSeconds = "14400"

// laneIdentity is one run's stand-in issuer and the catalog's token it signed.
type laneIdentity struct {
	Issuer       *dagger.Service
	CatalogToken *dagger.File
}

func (m *Rask) laneIdentity(src *dagger.Directory) laneIdentity {
	issuer := []string{"uv", "run", "--no-sync", "python", "scripts/lane_store_issuer.py"}
	keys := m.base(src).WithExec(append(issuer, "key", "/state")).Directory("/state")
	return laneIdentity{
		Issuer: m.base(src).
			WithDirectory("/state", keys).
			WithExposedPort(8080).
			WithDefaultArgs(append(issuer, "serve", "8080", laneIssuerURL, laneIssuer, "/state")).
			AsService(),
		CatalogToken: m.base(src).
			WithDirectory("/state", keys).
			WithExec(append(issuer, "mint", "/state", laneIssuer, laneCatalogSubject, laneStoreAudience, laneTokenSeconds, "/out/token")).
			File("/out/token"),
	}
}

// trustingLaneIssuer configures a RustFS container as the rustfs fixture configures the dev store
// (deploy/fixtures/rustfs): the issuer as a workload provider, the policy chosen by the token's subject, no role policy.
func trustingLaneIssuer(store *dagger.Container, issuer *dagger.Service) *dagger.Container {
	return store.
		WithServiceBinding("issuer", issuer).
		WithEnvVariable("RUSTFS_IDENTITY_OPENID_CONFIG_URL", laneIssuerURL+"/.well-known/openid-configuration").
		WithEnvVariable("RUSTFS_IDENTITY_OPENID_ISSUER", laneIssuer).
		WithEnvVariable("RUSTFS_IDENTITY_OPENID_CLIENT_ID", laneStoreAudience).
		WithEnvVariable("RUSTFS_IDENTITY_OPENID_CLAIM_NAME", "sub").
		WithEnvVariable("RUSTFS_IDENTITY_OPENID_HIDE_FROM_UI", "true").
		WithEnvVariable("RUSTFS_OUTBOUND_ALLOW_ORIGINS", laneIssuerURL)
}

// provisionLaneStore makes the catalog's bucket and grants the catalog's identity the canned policy the chart renders
// for it (`<release>-object-store-identities`), through `scripts/rustfs_admin.py`, the CLI the chart's provisioning hook
// runs. The store's root secret reaches that one exec as a mounted file, as the hook takes it.
func (m *Rask) provisionLaneStore(ctx context.Context, src *dagger.Directory, store *dagger.Service, host string) error {
	// The governed stack's own values (scripts/stack_values.sh, which install.go renders too), so the lane grants what a
	// release of that stack grants.
	render := `RELEASE=rask . scripts/stack_values.sh && helm template rask ./chart "${HELM_SET[@]}"` +
		" --api-versions external-secrets.io/v1/ExternalSecret --api-versions generators.external-secrets.io/v1alpha1/Password" +
		" --api-versions dapr.io/v1alpha1 --show-only templates/object-store-identities.yaml"
	_, err := withRenderableChart(m.base(src)).
		WithServiceBinding(host, store).
		WithMountedSecret("/run/secrets/rustfs/secret_key", dag.SetSecret("lane-store-root-secret", "rustfsadmin")).
		WithExec([]string{"bash", "-ceu", render + " | uv run --no-sync python scripts/lane_store_issuer.py policy catalog > /tmp/policy.json\n" +
			"uv run --no-sync python scripts/rustfs_admin.py --endpoint http://" + host + ":9000 --root-access-key rustfsadmin" +
			" --root-secret-file /run/secrets/rustfs/secret_key --bucket lance-catalog" +
			` --policy "` + laneCatalogSubject + `=$(cat /tmp/policy.json)"`}).
		Sync(ctx)
	return err
}

// withLaneStoreSession boots a catalog container on the session its token is exchanged for at the store `host`, as the
// chart's catalog boots (`LANCE_S3_WEB_IDENTITY_TOKEN_FILE`, `LANCE_S3_STS_ENDPOINT`), holding no store key.
func withLaneStoreSession(catalog *dagger.Container, token *dagger.File, host string) *dagger.Container {
	const path = "/var/run/secrets/rask/identity/" + laneStoreAudience + "/token"
	return catalog.
		WithMountedFile(path, token).
		WithEnvVariable("LANCE_S3_WEB_IDENTITY_TOKEN_FILE", path).
		WithEnvVariable("LANCE_S3_STS_ENDPOINT", "http://"+host+":9000")
}
