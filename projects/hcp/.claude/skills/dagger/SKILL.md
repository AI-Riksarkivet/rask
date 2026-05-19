---
name: building-with-dagger
description: >
  Write Dagger modules and functions in Go for container builds, CI/CD pipelines,
  and workflow automation. Use when: dagger, dagger module, dagger function,
  dagger call, dagger shell, dagger init, CI/CD pipeline as code, container build
  with dagger, publish container image, dagger SDK, daggerverse, dagger toolchain,
  dagger Go SDK, programmable CI, build-test-push pipeline, dagger.json,
  dag.Container, dag.Directory, multi-stage container build, dagger cloud,
  trivy scan, SBOM generation, vulnerability scanning, provenance attestation,
  cosign signing, supply chain security, docker compose dagger, service binding,
  dagger-for-github action, publish docker registry, publish pypi.
  Also use when the user wants to replace shell scripts or YAML CI configs with
  typed, composable Go code that runs identically locally and in CI.
---

# Dagger — Go SDK

Dagger is a programmable CI/CD engine that runs workflows in containers.
Pipelines are written as Go functions using a type-safe SDK, packaged into
modules, and executed via the Dagger CLI or Shell. The same code runs locally
and in any CI system — the only dependency is a container runtime.

## Quick reference

```
dagger init --sdk=go --name=myproject   # scaffold a new module
dagger develop                          # regenerate bindings after changes
dagger call <function> [--arg=val]      # call a function from CLI
dagger functions                        # list available functions
dagger                                  # launch interactive Dagger Shell
dagger install <module-ref>             # add a dependency module
dagger toolchain install <module-ref>   # add a toolchain
```

## Core concepts

**Functions** are the building blocks — regular Go methods on a struct that
become API-callable. They accept typed arguments (`*dagger.Directory`,
`*dagger.Container`, `*dagger.Secret`, `string`, etc.) and return typed values.

**Modules** package one or more functions. Initialized with `dagger init`,
they live in a directory with `dagger.json` + Go source under `.dagger/`.

**Chaining** — every function that returns a Dagger type (Container, Directory,
File, Service…) can be piped into the next. This is the main composition model.

**`dag`** is the pre-initialized Dagger client available in all module code.
It provides access to core types and installed dependencies.

## Go module structure

```
myproject/
├── dagger.json               # module metadata & dependencies
└── .dagger/
    ├── main.go               # main struct, constants, shared helpers
    ├── build.go              # build functions
    ├── test.go               # test functions
    ├── publish.go            # publish/release functions
    ├── scan.go               # vulnerability scanning
    ├── checks.go             # linting, formatting, type checking
    ├── serve.go              # service functions
    ├── dagger.gen.go         # auto-generated (do not edit)
    └── internal/
        └── dagger/           # generated SDK types
```

Split functions across files by concern — they all share the same `package main`.
Exported methods on the main struct become Dagger Functions. Unexported methods
are private helpers (useful for shared logic like base containers).

Use Go doc comments for descriptions. Key pragmas for arguments:
- `// +optional` — argument not required
- `// +default "value"` — default value
- `// +defaultPath="/"` — default to module root directory (for `*dagger.Directory`)

```go
// main.go — struct, constants, shared helpers
package main

import (
    "context"
    "dagger/myproject/internal/dagger"
)

type Myproject struct{}

const (
    DefaultRegistry  = "docker.io"
    DefaultImageRepo = "myorg/myapp"
)

// base returns a shared dev container (unexported = private helper)
func (m *Myproject) base(src *dagger.Directory) *dagger.Container {
    return dag.Container().
        From("golang:1.23").
        WithMountedDirectory("/src", src).
        WithWorkdir("/src").
        WithMountedCache("/go/pkg/mod", dag.CacheVolume("go-mod")).
        WithMountedCache("/root/.cache/go-build", dag.CacheVolume("go-build"))
}

// Build compiles the Go binary inside a container.
func (m *Myproject) Build(
    ctx context.Context,
    // +defaultPath="/"
    // +optional
    src *dagger.Directory,
    // +optional
    // +default "linux"
    os string,
    // +optional
    // +default "amd64"
    arch string,
) *dagger.File {
    return m.base(src).
        WithEnvVariable("GOOS", os).
        WithEnvVariable("GOARCH", arch).
        WithEnvVariable("CGO_ENABLED", "0").
        WithExec([]string{"go", "build", "-o", "/out/app", "."}).
        File("/out/app")
}
```

Call it: `dagger call build --os=linux --arch=arm64`

## Common patterns

For detailed Go patterns covering container builds, multi-stage images,
CI/CD pipelines, caching, secrets, and services:
→ See [references/go-patterns.md](references/go-patterns.md)

For vulnerability scanning (Trivy), SBOM generation, provenance extraction,
and supply-chain security patterns:
→ See [references/security-scanning.md](references/security-scanning.md)

For CLI commands, Dagger Shell usage, and CI integration:
→ See [references/cli-reference.md](references/cli-reference.md)

For Go SDK docs, official documentation links, and Daggerverse:
→ See [references/docs-and-resources.md](references/docs-and-resources.md)

## Key rules

1. **Functions are sandboxed** — no implicit host access. Pass directories,
   files, env vars, and secrets explicitly as arguments.
2. **Names convert to kebab-case** — `BuildAndPublish` in Go becomes
   `build-and-publish` on the CLI. `ScanCi` → `scan-ci`.
3. **`context.Context`** is needed as the first param for any function that
   materializes a value (returns `string`, `error`, etc.). Functions returning
   lazy types (`*dagger.Container`, `*dagger.Directory`) don't need it.
4. **`+defaultPath="/"`** on `*dagger.Directory` params makes source default
   to the module root. Combine with `// +optional` so callers can override.
5. **Run `dagger develop`** after changing dependencies or updating Dagger
   version to regenerate bindings. Check the changelog for breaking changes.
6. **Cache mounts** — use `dag.CacheVolume("key")` with `WithMountedCache`
   for package manager caches (pip, go mod, npm). Persists across runs.
7. **Secrets** — never hardcode. Accept `*dagger.Secret` as function argument
   and use `WithSecretVariable` or `WithMountedSecret` on containers.
8. **Split by concern** — put build, test, publish, scan, serve in separate
   .go files. They share `package main` and can call each other's methods.
9. **Unexported helpers** — lowercase methods (e.g. `base()`, `buildWithUv()`)
   are private helpers, not exposed as Dagger Functions. Use for shared logic.
10. **Service binding gotcha** — don't set env vars containing service
    hostnames (e.g. `OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318`).
    Dagger rewrites hostnames in env vars to tunnel addresses. Pass endpoints
    directly in code instead.

## Module dependencies

Install community modules from Daggerverse (https://daggerverse.dev/):

```bash
dagger install github.com/kpenfound/dagger-modules/golang@v0.2.1
```

Access in code via `dag`:
```go
func (m *Myproject) Test(ctx context.Context, src *dagger.Directory) (string, error) {
    return dag.Golang().WithProject(src).Test(ctx)
}
```

For ad-hoc usage, search tips, and more:
→ See [references/docs-and-resources.md](references/docs-and-resources.md)

## Toolchains (no-code modules)

Add pre-built functionality without writing module code:

```bash
dagger init
dagger toolchain install github.com/example/linter
dagger toolchain install github.com/example/tester
dagger call linter lint
dagger call tester test
```

## Dagger Shell

Interactive REPL for composing workflows:

```
$ dagger
> container | from alpine | with-exec apk add curl | stdout
> container | from alpine | terminal          # interactive debugging
> container | from alpine | .help             # context-sensitive help
```

## Core types quick reference

| Type | Go type | Description |
|------|---------|-------------|
| Container | `*dagger.Container` | OCI container — build, exec, publish, mount, export |
| Directory | `*dagger.Directory` | Filesystem dir — local path, git ref, or in-container |
| File | `*dagger.File` | Single file — contents, export, mount |
| Secret | `*dagger.Secret` | Credential — never in logs/cache |
| Service | `*dagger.Service` | Long-running process — TCP connectivity, bind to containers |
| CacheVolume | `*dagger.CacheVolume` | Persistent dir across runs — for pip/npm/go caches |
| GitRepository | `*dagger.GitRepository` | Git repo — branch, tag, commit, tree |
| LLM | `*dagger.LLM` | Large language model — native AI agent support |
| Env | `*dagger.Env` | Typed environment with inputs/outputs — for LLM workflows |
| CurrentModule | `*dagger.CurrentModule` | Module introspection — source dir, workdir |
| Socket | `*dagger.Socket` | Unix or TCP/IP socket — mount into containers |
| Terminal | `*dagger.Terminal` | Interactive terminal session |
| Port | `*dagger.Port` | Port exposed by a container |
| Platform | `dagger.Platform` | Target platform string — e.g. `"linux/amd64"` |

### Key methods by type

**Container** (most-used):
`From`, `Build`, `WithExec`, `WithDirectory`, `WithMountedDirectory`,
`WithFile`, `WithMountedFile`, `WithMountedCache`, `WithMountedSecret`,
`WithEnvVariable`, `WithSecretVariable`, `WithWorkdir`, `WithEntrypoint`,
`WithExposedPort`, `WithServiceBinding`, `WithRegistryAuth`,
`WithPlatformVariants`, `Stdout`, `Stderr`, `File`, `Directory`,
`Publish`, `Export`, `AsTarball`, `AsService`, `Terminal`, `Sync`

**Directory**:
`File`, `Entries`, `WithFile`, `WithFiles`, `WithDirectory`,
`DockerBuild`, `Export`

**File**:
`Contents`, `Export`

**Service**:
`Endpoint`, `Hostname`, `Ports`, `Up`

**Secret**:
`Name`, `Plaintext`

**CurrentModule**:
`Source`, `Workdir`, `WorkdirFile`

**GitRepository**:
`Branch`, `Tag`, `Commit`, `Head`, `Ref`, `Tags`, `Branches`

Every type has chainable methods. For the full method list on any type,
use `.help` in Dagger Shell: `container | from alpine | .help`

Option structs follow the pattern `<Type><Method>Opts`:
`dagger.ContainerBuildOpts`, `dagger.ContainerWithExecOpts`,
`dagger.ContainerWithDirectoryOpts`, `dagger.ContainerAsServiceOpts`, etc.

## Finding answers beyond this skill

Fastest method discovery: use `.help` in Dagger Shell:
```
$ dagger
> container | .help                          # all Container methods
> container | from alpine | .help with-exec  # specific method signature
```

For Go SDK docs (pkg.go.dev), official documentation (docs.dagger.io),
Daggerverse module search, and web search guidance:
→ See [references/docs-and-resources.md](references/docs-and-resources.md)
