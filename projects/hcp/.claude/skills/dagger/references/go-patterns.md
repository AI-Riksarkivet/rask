# Go SDK Patterns

## Contents
- Dockerfile-based builds with build args
- Tool injection (copying binaries from tool images)
- Code quality checks (linting, formatting, type checking)
- Testing with service dependencies
- CI/CD pipeline (test → build → push)
- Publishing to Docker registries with auth
- Publishing to PyPI
- Docker Compose integration
- Service functions and health checks
- Version management and validation
- Caching strategies
- Secrets handling
- Multi-platform builds
- Container image builds (no Dockerfile)

---

## Dockerfile-based builds with build args

When a Dockerfile already exists, use `Container().Build()` with build args:

```go
func (m *Myproject) BuildLocal(
    ctx context.Context,
    // +defaultPath="/"
    // +optional
    source *dagger.Directory,
    // +default="python:3.13-alpine"
    baseImage string,
    // Environment variables for build customization (KEY=VALUE format)
    // +default=[]
    envVars []string,
    // +default="docker.io"
    registry string,
) (*dagger.Container, error) {
    buildArgs := []dagger.BuildArg{
        {Name: "REGISTRY", Value: registry},
        {Name: "BASE_IMAGE", Value: baseImage},
    }

    for _, envVar := range envVars {
        if parts := strings.Split(envVar, "="); len(parts) == 2 {
            buildArgs = append(buildArgs, dagger.BuildArg{
                Name:  parts[0],
                Value: parts[1],
            })
        }
    }

    return dag.Container().
        Build(source, dagger.ContainerBuildOpts{
            Dockerfile: ".docker/myapp.dockerfile",
            BuildArgs:  buildArgs,
        }), nil
}

// Build wraps BuildLocal with sensible defaults
func (m *Myproject) Build(
    ctx context.Context,
    // +defaultPath="/"
    // +optional
    source *dagger.Directory,
    // +default="python:3.13-alpine"
    // +optional
    baseImage string,
) (*dagger.Container, error) {
    if baseImage == "" {
        baseImage = "python:3.13-alpine"
    }
    return m.BuildLocal(ctx, source, DefaultImageRepo, baseImage, []string{}, DefaultRegistry)
}
```

## Tool injection (copying binaries from tool images)

Copy standalone binaries from tool containers into your build.
Avoids installing package managers in the target image:

```go
func (m *Myproject) withUv(container *dagger.Container) *dagger.Container {
    uvBinary := dag.Container().
        From("ghcr.io/astral-sh/uv:latest").
        File("/uv")
    uvxBinary := dag.Container().
        From("ghcr.io/astral-sh/uv:latest").
        File("/uvx")

    return container.
        WithFile("/usr/local/bin/uv", uvBinary).
        WithFile("/usr/local/bin/uvx", uvxBinary)
}

func (m *Myproject) buildWithUv(ctx context.Context, source *dagger.Directory) (*dagger.Container, error) {
    container := dag.Container().
        From("python:3.13-alpine").
        WithDirectory("/app", source).
        WithWorkdir("/app")

    container = m.withUv(container)
    container = container.WithExec([]string{"uv", "sync", "--frozen", "--no-cache"})
    return container, nil
}
```

Works for any tool: crane, trivy, golangci-lint, etc.

## Code quality checks

Define commands as vars, run fix-then-verify in sequence:

```go
var (
    ruffFormatCmd      = []string{"uvx", "ruff", "format", "."}
    ruffFormatCheckCmd = []string{"uvx", "ruff", "format", "--check", "."}
    ruffCheckFixCmd    = []string{"uvx", "ruff", "check", "--fix", "."}
    ruffCheckCmd       = []string{"uvx", "ruff", "check", "."}
    pipAuditCmd        = []string{"uvx", "pip-audit", "--strict", "--desc"}
)

func (m *Myproject) Checks(
    ctx context.Context,
    // +defaultPath="/"
    // +optional
    source *dagger.Directory,
) (string, error) {
    container, err := m.buildWithUv(ctx, source)
    if err != nil {
        return "", err
    }

    _, err = container.
        WithExec(ruffFormatCmd).
        WithExec(ruffCheckFixCmd).
        WithExec(ruffFormatCheckCmd).
        WithExec(ruffCheckCmd).
        WithExec(pipAuditCmd).
        Sync(ctx)

    if err != nil {
        return "", fmt.Errorf("checks failed: %w", err)
    }
    return "All checks passed", nil
}
```

Individual functions can return `*dagger.Directory` with fixed source.

## Testing with service dependencies

Use `WithServiceBinding` for integration tests against real services:

```go
func (m *Myproject) TestServer(
    ctx context.Context,
    // +defaultPath="/"
    // +optional
    source *dagger.Directory,
    // +default=7860
    port int,
) (string, error) {
    service, err := m.Serve(ctx, source, port, "0.0.0.0")
    if err != nil {
        return "", fmt.Errorf("failed to start service: %w", err)
    }

    output, err := dag.Container().
        From("curlimages/curl:latest").
        WithServiceBinding("myapp", service).
        WithExec([]string{
            "curl", "-f", "-s",
            fmt.Sprintf("http://myapp:%d/health", port),
        }).
        Stdout(ctx)
    if err != nil {
        return "", fmt.Errorf("health check failed: %w", err)
    }
    return fmt.Sprintf("Server healthy\n%s", output), nil
}
```

## CI/CD pipeline (test → build → push)

Private helpers compose test + build steps:

```go
func (m *Myproject) testAndBuild(
    ctx context.Context,
    source *dagger.Directory,
    baseImage string,
    operation string,
) (*dagger.Container, error) {
    _, err := m.Test(ctx, source, baseImage)
    if err != nil {
        return nil, fmt.Errorf("tests failed, aborting %s: %w", operation, err)
    }
    container, err := m.Build(ctx, source, baseImage)
    if err != nil {
        return nil, fmt.Errorf("build failed during %s: %w", operation, err)
    }
    return container, nil
}
```

## Publishing to Docker registries with auth

```go
func (m *Myproject) PublishDocker(
    ctx context.Context,
    // +default="myorg/myapp"
    imageRepository string,
    // +optional
    tag string,
    // +default="python:3.13-alpine"
    // +optional
    baseImage string,
    // +optional
    tagSuffix string,
    // +default="docker.io"
    registry string,
    dockerUsername *dagger.Secret,
    dockerPassword *dagger.Secret,
    // +defaultPath="/"
    // +optional
    source *dagger.Directory,
    // +optional
    skipValidation bool,
) (string, error) {
    resolvedTag, err := m.resolveTag(ctx, source, tag, skipValidation)
    if err != nil {
        return "", err
    }
    if tagSuffix != "" {
        resolvedTag = resolvedTag + tagSuffix
    }

    container, err := m.testAndBuild(ctx, source, baseImage, "Docker publish")
    if err != nil {
        return "", err
    }

    imageRef := registry + "/" + imageRepository + ":" + resolvedTag

    if dockerPassword != nil && dockerUsername != nil {
        username, err := dockerUsername.Plaintext(ctx)
        if err != nil {
            return "", fmt.Errorf("failed to read docker username: %w", err)
        }
        return container.
            WithRegistryAuth(registry, username, dockerPassword).
            Publish(ctx, imageRef)
    }
    return container.Publish(ctx, imageRef)
}
```

Call:
```bash
dagger call publish-docker \
    --docker-username=env:DOCKER_USER \
    --docker-password=env:DOCKER_TOKEN \
    --tag=v1.2.3
```

## Publishing to PyPI

```go
func (m *Myproject) PublishPypi(
    ctx context.Context,
    // +defaultPath="/"
    // +optional
    source *dagger.Directory,
    // +default="https://upload.pypi.org/legacy/"
    publishUrl string,
    // +default="__token__"
    username string,
    pypiToken *dagger.Secret,
) (string, error) {
    container, err := m.buildWithUv(ctx, source)
    if err != nil {
        return "", err
    }

    return container.
        WithExec([]string{"uv", "build"}).
        WithSecretVariable("UV_PUBLISH_PASSWORD", pypiToken).
        WithExec([]string{
            "uv", "publish",
            "--publish-url", publishUrl,
            "--username", username,
        }).
        Stdout(ctx)
}
```

## Docker Compose integration

Install the community docker-compose module from Daggerverse:

```bash
dagger install github.com/shykes/daggerverse/docker-compose@f82c283510bac0399451dff7ffbec0274bfc3bd4
```

The module provides a native Dagger reimplementation of Docker Compose.
Current compatibility: image, build, ports, environment, entrypoint, command.

### API surface

```go
// Load a project from a directory containing docker-compose.yml
project := dag.DockerCompose().Project(dagger.DockerComposeProjectOpts{
    Source: source.Directory(".docker"),
})

// Get project config
config, _ := project.Config(ctx)          // raw YAML string
configFile := project.ConfigFile()         // *dagger.File

// Work with individual services
svc := project.Service("myapp")
svc.Config(ctx)                            // service YAML
svc.Container()                            // *dagger.Container (with compose config applied)
svc.BaseContainer()                        // *dagger.Container (without compose modifications)
svc.Up()                                   // *dagger.Service (start the service)

// List all services
services, _ := project.Services()          // []*DockerComposeComposeService
```

### Starting a compose service and health-checking it

```go
func (m *Myproject) ComposeUp(
    // +defaultPath="/"
    // +optional
    source *dagger.Directory,
) *dagger.Service {
    project := dag.DockerCompose().Project(dagger.DockerComposeProjectOpts{
        Source: source.Directory(".docker"),
    })
    return project.Service("myapp").Up()
}

func (m *Myproject) ComposeTest(
    ctx context.Context,
    // +defaultPath="/"
    // +optional
    source *dagger.Directory,
) (string, error) {
    service := m.ComposeUp(source)

    output, err := dag.Container().
        From("curlimages/curl:latest").
        WithServiceBinding("myapp", service).
        WithExec([]string{"curl", "-f", "-s", "http://myapp:7860/health"}).
        Stdout(ctx)
    if err != nil {
        return "", fmt.Errorf("compose health check failed: %w", err)
    }
    return output, nil
}
```

Note: The `Source` directory should point to the folder containing
`docker-compose.yml`, not the project root (unless compose file is at root).

## Service functions and health checks

Expose a built container as a long-running service:

```go
func (m *Myproject) Serve(
    ctx context.Context,
    // +defaultPath="/"
    // +optional
    source *dagger.Directory,
    // +default=7860
    port int,
    // +default="0.0.0.0"
    host string,
) (*dagger.Service, error) {
    container, err := m.Build(ctx, source, "")
    if err != nil {
        return nil, err
    }

    return container.
        WithExposedPort(port).
        AsService(dagger.ContainerAsServiceOpts{
            Args: []string{"myapp", "--host", host, "--port", fmt.Sprintf("%d", port)},
        }), nil
}

// ServePublished runs a pre-built image from a registry
func (m *Myproject) ServePublished(
    ctx context.Context,
    // +default="myorg/myapp:latest"
    imageRef string,
    // +default=7860
    port int,
) (*dagger.Service, error) {
    return dag.Container().
        From(imageRef).
        WithExposedPort(port).
        AsService(dagger.ContainerAsServiceOpts{
            Args: []string{"myapp", "--host", "0.0.0.0", "--port", fmt.Sprintf("%d", port)},
        }), nil
}
```

## Version management and validation

Read version from project metadata, validate against release tags:

```go
func (m *Myproject) getVersion(ctx context.Context, source *dagger.Directory) (string, error) {
    container := dag.Container().
        From("python:3.13-alpine").
        WithDirectory("/app", source).
        WithWorkdir("/app")
    container = m.withUv(container)

    version, err := container.
        WithExec([]string{"uv", "version", "--short"}).
        Stdout(ctx)
    if err != nil {
        return "", err
    }
    return strings.TrimSpace(version), nil
}

func (m *Myproject) validateVersion(ctx context.Context, source *dagger.Directory, tag string) error {
    projectVersion, err := m.getVersion(ctx, source)
    if err != nil {
        return fmt.Errorf("failed to get version: %w", err)
    }
    normalize := func(v string) string {
        return strings.TrimPrefix(strings.TrimSpace(v), "v")
    }
    if normalize(projectVersion) != normalize(tag) {
        return fmt.Errorf("version mismatch: project has v%s but tag is %s",
            projectVersion, tag)
    }
    return nil
}

func (m *Myproject) resolveTag(ctx context.Context, source *dagger.Directory, tag string, skipValidation bool) (string, error) {
    if tag == "" {
        version, err := m.getVersion(ctx, source)
        if err != nil {
            return "", err
        }
        return "v" + version, nil
    }
    if !skipValidation {
        if err := m.validateVersion(ctx, source, tag); err != nil {
            return "", err
        }
    }
    return tag, nil
}
```

## Caching strategies

```go
// Go modules
container.
    WithMountedCache("/go/pkg/mod", dag.CacheVolume("go-mod")).
    WithMountedCache("/root/.cache/go-build", dag.CacheVolume("go-build"))

// pip / uv
container.WithMountedCache("/root/.cache/pip", dag.CacheVolume("pip"))

// npm
container.WithMountedCache("/root/.npm", dag.CacheVolume("npm"))

// apt
container.WithMountedCache("/var/cache/apt", dag.CacheVolume("apt"))
```

## Secrets handling

```go
// Registry auth
container.WithRegistryAuth(registry, username, passwordSecret)

// Env var secret (never in logs or cache keys)
container.WithSecretVariable("UV_PUBLISH_PASSWORD", pypiToken)

// File-mounted secret
container.WithMountedSecret("/run/secrets/token", tokenSecret)
```

From CLI:
```bash
--token=env:MY_TOKEN          # from env var
--token=file:./secret.txt     # from file
--token=cmd:"vault read ..."  # from command
```

## Multi-platform builds

```go
func (m *Myproject) BuildMultiPlatform(src *dagger.Directory) *dagger.Container {
    variants := make([]*dagger.Container, 0)
    for _, platform := range []dagger.Platform{"linux/amd64", "linux/arm64"} {
        binary := dag.Container(dagger.ContainerOpts{Platform: platform}).
            From("golang:1.23").
            WithMountedDirectory("/src", src).
            WithWorkdir("/src").
            WithEnvVariable("CGO_ENABLED", "0").
            WithExec([]string{"go", "build", "-o", "/out/app", "."}).
            File("/out/app")

        ctr := dag.Container(dagger.ContainerOpts{Platform: platform}).
            From("gcr.io/distroless/static-debian12:latest").
            WithFile("/app", binary).
            WithEntrypoint([]string{"/app"})
        variants = append(variants, ctr)
    }
    return dag.Container().WithPlatformVariants(variants)
}
```

## Container image builds (no Dockerfile)

```go
func (m *Myproject) BuildImage(src *dagger.Directory) *dagger.Container {
    binary := dag.Container().
        From("golang:1.23-alpine").
        WithMountedDirectory("/src", src).
        WithWorkdir("/src").
        WithEnvVariable("CGO_ENABLED", "0").
        WithExec([]string{"go", "build", "-ldflags", "-s -w", "-o", "/app", "."}).
        File("/app")

    return dag.Container().
        From("gcr.io/distroless/static-debian12:latest").
        WithFile("/app", binary).
        WithEntrypoint([]string{"/app"})
}
```
