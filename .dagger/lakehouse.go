package main

import (
	"context"
	"strings"

	"dagger/rask/internal/dagger"
)

// lakehouseMembers are the workspace members the lakehouse is: the import packages `.importlinter` names as its root
// packages. Every other member under services/ and packages/ is a client's and is deleted, so a new client member needs
// no edit here.
//
// clientTrees is every client tree outside services/ and packages/: the sealed runners, the web plane and its Playwright
// suite, and the fixtures of the clients' own releases; clientImages is each client image's dockerfile. A new client
// tree there is added to these lists, as its modules are to `.importlinter`'s `the-lakehouse-knows-no-client`.
//
// Deleting all of them is the removal test (XC-175): the lakehouse is the product when its gates hold without any client.
var (
	lakehouseMembers = map[string]bool{
		"services/catalog":       true,
		"services/lineage":       true,
		"services/maintenance":   true,
		"packages/lakehouse-kit": true,
		"packages/lineage-kit":   true,
		"packages/service-kit":   true,
		"packages/storage":       true,
	}
	clientTrees = []string{
		"frontend",
		"runners",
		"tests/e2e",
		"deploy/fixtures/annotator",
		"deploy/fixtures/compute",
		"deploy/fixtures/kuberay-operator",
		"deploy/fixtures/mailpit",
		"deploy/fixtures/nvidia-device-plugin",
		"deploy/fixtures/ray-cluster",
		"deploy/fixtures/table-maintenance",
	}
	clientImages = []string{
		".docker/annotator.dockerfile",
		".docker/assist-runner.dockerfile",
		".docker/compute.dockerfile",
		".docker/flows.dockerfile",
		".docker/frontend.dockerfile",
		".docker/media.dockerfile",
		".docker/notifications.dockerfile",
		".docker/ray-cluster.dockerfile",
		".docker/ray-runner.dockerfile",
		".docker/runner.dockerfile",
		".docker/table-maintenance.dockerfile",
	}
)

// LakehouseAlone runs the lakehouse's own gates in a tree with every client deleted (XC-188): the import contracts
// (`make lint-imports`, which runs a client's own contract only where that client's tree is present) and the offline
// suite the Test lane runs, on a workspace re-locked without the clients. It goes red when a lakehouse module, test,
// contract or gate needs a client's tree.
//
//	dagger call lakehouse-alone
func (m *Rask) LakehouseAlone(
	ctx context.Context,
	// +defaultPath="/"
	// +optional
	src *dagger.Directory,
) (string, error) {
	alone := src.WithoutFiles(clientImages)
	for _, root := range []string{"services", "packages"} {
		members, err := src.Directory(root).Entries(ctx)
		if err != nil {
			return "", err
		}
		for _, member := range members {
			if path := root + "/" + strings.TrimSuffix(member, "/"); !lakehouseMembers[path] {
				alone = alone.WithoutDirectory(path)
			}
		}
	}
	for _, tree := range clientTrees {
		alone = alone.WithoutDirectory(tree)
	}
	return m.base(alone).
		With(withRenderableChart).
		WithFile("/usr/local/bin/openfga", dag.Container().From(openfgaImage).File("/openfga")).
		WithExec([]string{"make", "lint-imports"}).
		WithExec([]string{"uv", "run", "--no-sync", "pytest", "-q", "--timeout=300", "--timeout-method=thread", "-m", "not e2e and not slow"}).
		Stdout(ctx)
}
