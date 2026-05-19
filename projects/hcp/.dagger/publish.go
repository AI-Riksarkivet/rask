package main

import (
	"context"
	"fmt"

	"dagger/ra-hcp/internal/dagger"
)

const (
	defaultRegistry      = "docker.io"
	defaultBackendRepo   = "riksarkivet/ra-hcp"
	defaultFrontendRepo  = "riksarkivet/ra-hcp-frontend"
)

// PublishBackend builds and publishes the backend container image to a Docker registry.
func (m *RaHcp) PublishBackend(
	ctx context.Context,
	// +defaultPath="/"
	source *dagger.Directory,
	// Image tag (e.g. "latest", "v0.3.0")
	// +default="latest"
	tag string,
	// Docker registry
	// +default="docker.io"
	// +optional
	registry string,
	// Image repository
	// +default="riksarkivet/ra-hcp"
	// +optional
	imageRepository string,
	// Docker username for authentication
	// +optional
	dockerUsername *dagger.Secret,
	// Docker password for authentication
	// +optional
	dockerPassword *dagger.Secret,
) (string, error) {
	container := m.BuildBackend(source)
	imageRef := registry + "/" + imageRepository + ":" + tag

	if dockerUsername != nil && dockerPassword != nil {
		username, err := dockerUsername.Plaintext(ctx)
		if err != nil {
			return "", fmt.Errorf("failed to read docker username: %w", err)
		}
		container = container.WithRegistryAuth(registry, username, dockerPassword)
	}

	return container.Publish(ctx, imageRef)
}

// PublishFrontend builds and publishes the frontend container image to a Docker registry.
func (m *RaHcp) PublishFrontend(
	ctx context.Context,
	// +defaultPath="/"
	source *dagger.Directory,
	// Image tag (e.g. "latest", "v0.3.0")
	// +default="latest"
	tag string,
	// Docker registry
	// +default="docker.io"
	// +optional
	registry string,
	// Image repository
	// +default="riksarkivet/ra-hcp-frontend"
	// +optional
	imageRepository string,
	// Docker username for authentication
	// +optional
	dockerUsername *dagger.Secret,
	// Docker password for authentication
	// +optional
	dockerPassword *dagger.Secret,
) (string, error) {
	container := m.BuildFrontend(source)
	imageRef := registry + "/" + imageRepository + ":" + tag

	if dockerUsername != nil && dockerPassword != nil {
		username, err := dockerUsername.Plaintext(ctx)
		if err != nil {
			return "", fmt.Errorf("failed to read docker username: %w", err)
		}
		container = container.WithRegistryAuth(registry, username, dockerPassword)
	}

	return container.Publish(ctx, imageRef)
}

// PublishAll builds and publishes both backend and frontend images.
func (m *RaHcp) PublishAll(
	ctx context.Context,
	// +defaultPath="/"
	source *dagger.Directory,
	// Image tag (e.g. "latest", "v0.3.0")
	// +default="latest"
	tag string,
	// Docker registry
	// +default="docker.io"
	// +optional
	registry string,
	// Backend image repository
	// +default="riksarkivet/ra-hcp"
	// +optional
	backendRepo string,
	// Frontend image repository
	// +default="riksarkivet/ra-hcp-frontend"
	// +optional
	frontendRepo string,
	// Docker username for authentication
	// +optional
	dockerUsername *dagger.Secret,
	// Docker password for authentication
	// +optional
	dockerPassword *dagger.Secret,
) (string, error) {
	backendRef, err := m.PublishBackend(ctx, source, tag, registry, backendRepo, dockerUsername, dockerPassword)
	if err != nil {
		return "", fmt.Errorf("backend publish failed: %w", err)
	}

	frontendRef, err := m.PublishFrontend(ctx, source, tag, registry, frontendRepo, dockerUsername, dockerPassword)
	if err != nil {
		return "", fmt.Errorf("frontend publish failed: %w", err)
	}

	return fmt.Sprintf("Published:\n  backend:  %s\n  frontend: %s", backendRef, frontendRef), nil
}
