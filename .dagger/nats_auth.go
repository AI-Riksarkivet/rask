package main

import (
	"context"
	"encoding/json"

	"dagger/rask/internal/dagger"
)

// natsBoxImage is the nats-box the estate deploys: the NATS fixture's operator box, and the openbao fixture's `mintImage`,
// whose nsc the dev mint runs. The lane mints its test keys with this image's nsc, so they come from the same tool.
const natsBoxImage = "natsio/nats-box:0.20.2"

// natsStreamJobImage is the image chart/templates/nats-stream-job.yaml runs the stream Job in. The lane runs that
// Job's rendered scripts, both phases, with this image's nats CLI, as the `admin` user.
const natsStreamJobImage = "natsio/nats-box:0.20.2"

// busSignerDockerfile builds the NATS fixture's stand-in signer image; its `build` stage is the binary the lane runs.
const busSignerDockerfile = ".docker/bus-signer.dockerfile"

// NatsAuth proves the chart's NATS permission table (`nats.auth.users`) on a real bus: the NATS fixture's three
// nats-servers clustered in operator mode with a MEMORY resolver and JetStream, each stream at the chart's default
// replicas as on the estate, TLS on the client port, test-only keys minted with nsc, a static user per row that is no
// session user's, and for each session user a scoped signing key its pod's session is signed with by the fixture's
// stand-in signer (two of them, against a TokenReview the test answers). Each service's own connection
// (`lakehouse.events.bus`) presents only its pod's token, publishes, pulls its durable consumer and follows its subject
// against it, renews its session, and the chart's stream Job declares and retires the streams and consumers
// (tests/e2e-py/test_nats_admits_each_client_only_to_its_own_subjects.py says what it asserts). On the same rig, the
// maintenance worker on the queue the chart's stream Job declares, as its own bus user: two replicas share a burst, a
// killed one's task goes to the other, and its commit lane settles each announcement
// (tests/e2e-py/test_two_workers_share_the_queue_and_a_dead_ones_task_goes_to_the_other.py).
//
//	dagger call nats-auth
//	dagger call nats-auth --values=<a values file carrying nats.auth.users>
//
// THE BINARIES ARE COPIED OUT OF THE IMAGES THE CHART DEPLOYS and run as processes beside pytest, not as
// Dagger services. The test writes the broker's config from keys it mints inside the run, reads the
// broker's own log to judge every refusal, stops and starts the signers, and cuts a client's connection
// through a relay of its own; a service binding gives the test none of these. Every binary here is a
// static Go build, so it runs in the uv image unchanged. The signer is built, not copied: the `build`
// stage of the dockerfile its fixture image is built from compiles it (`CGO_ENABLED=0`, the golang image
// that dockerfile pins). `images.json` names the image each binary came from, or the dockerfile the
// signer was built from, and the test refuses a lane whose images the chart and the NATS fixture do not
// deploy, so these pins cannot drift from them silently.
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
		"bus-signer":  busSignerDockerfile,
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
		WithFile("bus-signer", src.DockerBuild(dagger.DirectoryDockerBuildOpts{Dockerfile: busSignerDockerfile, Target: "build"}).File("/out/bus-signer"), executable).
		WithNewFile("images.json", string(manifest))

	return m.base(src).
		With(withRenderableChart).
		WithDirectory("/opt/nats-auth", tools).
		WithFile("/opt/nats-auth/values.yaml", values).
		WithEnvVariable("RASK_NATS_AUTH_TOOLS", "/opt/nats-auth").
		WithEnvVariable("RASK_NATS_AUTH_VALUES", "/opt/nats-auth/values.yaml").
		// A hang detector, as in Test: a stuck broker or client would otherwise print nothing at all, because
		// Dagger buffers an exec's output until it completes. The permission proof runs in a few minutes and the
		// worker's about as long (it waits out a follow-up's 60 s retry).
		// `--require-live`: a skip in a lane that stood its stack up is a test that ran nowhere, so it fails the lane.
		WithExec([]string{
			"uv", "run", "--no-sync", "pytest", "tests/e2e-py/test_nats_admits_each_client_only_to_its_own_subjects.py",
			"tests/e2e-py/test_two_workers_share_the_queue_and_a_dead_ones_task_goes_to_the_other.py",
			"-m", "nats_auth", "-v", "-rs", "-p", "no:cacheprovider", "--timeout=600", "--timeout-method=thread", "--require-live",
		}).
		Stdout(ctx)
}
