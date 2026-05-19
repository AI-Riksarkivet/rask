package main

import (
	"dagger/ra-hcp/internal/dagger"
)

// BuildBackend builds the backend container from .docker/backend.dockerfile.
func (m *RaHcp) BuildBackend(source *dagger.Directory) *dagger.Container {
	return source.
		DockerBuild(dagger.DirectoryDockerBuildOpts{
			Dockerfile: ".docker/backend.dockerfile",
		})
}

// BuildFrontend builds the frontend container from .docker/frontend.dockerfile.
func (m *RaHcp) BuildFrontend(source *dagger.Directory) *dagger.Container {
	return source.
		DockerBuild(dagger.DirectoryDockerBuildOpts{
			Dockerfile: ".docker/frontend.dockerfile",
		})
}
