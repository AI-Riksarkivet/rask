# Docs & Resources

Use these resources when the skill's patterns don't cover a specific
method, option struct, or advanced feature. Listed in priority order.

---

## Dagger Shell `.help`

Interactive discovery — fastest way to find methods on any type:
```
$ dagger
> container | .help                          # all Container methods
> container | from alpine | .help with-exec  # specific method signature
> directory | .help                          # all Directory methods
> secret | .help                            # all Secret methods
> .help                                     # top-level functions
```

## Go SDK reference (pkg.go.dev)

Canonical source of truth for method signatures and option structs:
→ https://pkg.go.dev/dagger.io/dagger

Search for types: `Container`, `Directory`, `File`, `Secret`, `Service`,
`CacheVolume`, `GitRepository`, `LLM`, `Env`, `CurrentModule`.

Option structs follow the pattern `<Type><Method>Opts`:
`ContainerBuildOpts`, `ContainerWithExecOpts`, `ContainerWithDirectoryOpts`,
`ContainerAsServiceOpts`, `ContainerWithFileOpts`, etc.

## Official documentation (docs.dagger.io)

Conceptual guides and tutorials organized by topic:

**Core types** (each type has its own sub-page with examples):
→ https://docs.dagger.io/getting-started/types
  - Container: https://docs.dagger.io/getting-started/types/container
  - Directory: https://docs.dagger.io/getting-started/types/directory
  - File: https://docs.dagger.io/getting-started/types/file
  - Secret: https://docs.dagger.io/getting-started/types/secret
  - Service: https://docs.dagger.io/getting-started/types/service
  - CacheVolume: https://docs.dagger.io/getting-started/types/cachevolume
  - LLM: https://docs.dagger.io/getting-started/types/llm
  - Env: https://docs.dagger.io/getting-started/types/env
  - GitRepository: https://docs.dagger.io/getting-started/types/git

**Writing modules & functions**:
  - Functions: https://docs.dagger.io/extending/functions
  - Arguments & pragmas: https://docs.dagger.io/extending/arguments
  - Return types: https://docs.dagger.io/extending/return-types
  - Chaining: https://docs.dagger.io/extending/chaining
  - Module dependencies: https://docs.dagger.io/extending/module-dependencies
  - Custom types: https://docs.dagger.io/extending

**Core concepts**:
  - Toolchains & checks: https://docs.dagger.io/core-concepts/toolchains
  - Functions: https://docs.dagger.io/core-concepts/functions

**Recipes & integrations**:
  - Cookbook: https://docs.dagger.io/cookbook
  - GitHub Actions: https://docs.dagger.io/getting-started/ci-integrations/github-actions
  - GitLab CI: https://docs.dagger.io/getting-started/ci-integrations/gitlab
  - Use cases: https://docs.dagger.io/use-cases

**Full API reference**: https://docs.dagger.io/reference/

## Daggerverse (community modules)

Before writing something from scratch, check if a module already exists:
→ https://daggerverse.dev/

Search for common tasks: golang, python, node, docker-compose, trivy,
helm, golangci-lint, argocd, terraform, and many more.

### Notable modules

**docker-compose** — native Dagger reimplementation of Docker Compose:
```bash
dagger install github.com/shykes/daggerverse/docker-compose@f82c283510bac0399451dff7ffbec0274bfc3bd4
```
API: `dag.DockerCompose().Project(source).Service("name").Up()` — see
go-patterns.md for full usage.

### Using modules ad-hoc (without installing)

```bash
dagger -m github.com/kpenfound/dagger-modules/golang@v0.2.1 \
    call build --source=.
```

### Installing as a dependency

```bash
# Adds to dagger.json
dagger install github.com/kpenfound/dagger-modules/golang@v0.2.1
```

Then use in Go code via `dag`:
```go
func (m *Myproject) Test(ctx context.Context, src *dagger.Directory) (string, error) {
    return dag.Golang().WithProject(src).Test(ctx)
}
```

Modules on Daggerverse have documentation pages showing available
functions, arguments, and usage examples. Always pin to a version tag.

### Listing installed dependencies

```bash
cat dagger.json  # shows module dependencies
dagger functions  # lists all functions including from dependencies
```

## When to search the web

Search the web when you need:
- A method or type not documented in this skill or references
- Version-specific API changes (the Dagger API evolves across versions)
- Integration patterns for specific tools (e.g. Argo CD, Terraform, Helm)
- Community examples for uncommon workflows
- Troubleshooting errors or unexpected behavior

Check the SDK version in `go.mod` (e.g. `dagger.io/dagger v0.19.x`)
and search accordingly — some methods are added or changed between versions.
