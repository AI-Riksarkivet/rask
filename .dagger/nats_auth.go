package main

import (
	"context"
	"encoding/json"

	"dagger/rask/internal/dagger"
)

// natsBoxImage is the nats-box the chart deploys: the nats subchart's operator box, and `signing.mintImage`, whose
// nsc the dev mint runs. The lane mints its test keys with this image's nsc, so they come from the same tool.
const natsBoxImage = "natsio/nats-box:0.19.7"

// natsStreamJobImage is the image chart/templates/nats-stream-job.yaml runs the stream Job in. The lane runs that
// Job's rendered script with this image's nats CLI, as the `admin` user.
const natsStreamJobImage = "natsio/nats-box:0.14.5"

// daprdImage is the sidecar the dapr 1.18.1 subchart's injector stamps into every pod (its SIDECAR_IMAGE).
const daprdImage = "ghcr.io/dapr/daprd:1.18.1"

// NatsAuth proves the chart's NATS permission table (`nats.auth.users`) on a real broker: the chart's
// nats-server in operator mode with a MEMORY resolver and JetStream, test-only keys minted with nsc, one user
// per table row, and real daprd 1.18.1 running the chart's own Components against it
// (tests/e2e-py/test_nats_admits_each_client_only_to_its_own_subjects.py says what it asserts).
//
//	dagger call nats-auth
//	dagger call nats-auth --values=<a values file carrying nats.auth.users>
//
// THE BINARIES ARE COPIED OUT OF THE IMAGES THE CHART DEPLOYS and run as processes beside pytest, not as
// Dagger services. The test writes the broker's config from keys it mints inside the run, reads the
// broker's own log to judge every refusal, and starts one daprd per Component shape against a resources
// directory it renders; a service binding gives the test none of the three. Every binary here is a static
// Go build, so it runs in the uv image unchanged. `images.json` names the image each came from, and the test
// refuses a lane whose images the chart does not deploy, so these pins cannot drift from the chart silently.
//
// `--values` replaces only the file the table is read from; the chart itself is always the source tree's.
func (m *Rask) NatsAuth(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
	// The values file the permission table is read from, under `nats.auth.users`.
	// +defaultPath="/chart/values.yaml"
	// +optional
	values *dagger.File,
) (string, error) {
	manifest, err := json.Marshal(map[string]string{
		"nats-server": natsImage,
		"nsc":         natsBoxImage,
		"box/nats":    natsBoxImage,
		"job/nats":    natsStreamJobImage,
		"daprd":       daprdImage,
	})
	if err != nil {
		return "", err
	}
	executable := dagger.DirectoryWithFileOpts{Permissions: 0o755}
	tools := dag.Directory().
		WithFile("nats-server", dag.Container().From(natsImage).File("/usr/local/bin/nats-server"), executable).
		WithFile("nsc", dag.Container().From(natsBoxImage).File("/usr/local/bin/nsc"), executable).
		WithFile("box/nats", dag.Container().From(natsBoxImage).File("/usr/local/bin/nats"), executable).
		WithFile("job/nats", dag.Container().From(natsStreamJobImage).File("/usr/local/bin/nats"), executable).
		WithFile("daprd", dag.Container().From(daprdImage).File("/daprd"), executable).
		WithNewFile("images.json", string(manifest))

	return m.base(src).
		With(withRenderableChart).
		WithDirectory("/opt/nats-auth", tools).
		WithFile("/opt/nats-auth/values.yaml", values).
		WithEnvVariable("RASK_NATS_AUTH_TOOLS", "/opt/nats-auth").
		WithEnvVariable("RASK_NATS_AUTH_VALUES", "/opt/nats-auth/values.yaml").
		// A hang detector, as in Test: a stuck broker or sidecar would otherwise print nothing at all, because
		// Dagger buffers an exec's output until it completes. The test's measured runtime is under a minute.
		WithExec([]string{
			"uv", "run", "--no-sync", "pytest", "tests/e2e-py/test_nats_admits_each_client_only_to_its_own_subjects.py",
			"-m", "nats_auth", "-v", "-p", "no:cacheprovider", "--timeout=600", "--timeout-method=thread",
		}).
		Stdout(ctx)
}
