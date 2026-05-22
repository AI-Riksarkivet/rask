# CLI & Dagger Shell Reference

## Contents
- Installation
- CLI commands
- Dagger Shell (interactive REPL)
- CI integration (GitHub Actions, GitLab)
- Module management
- Debugging & observability

---

## Installation

```bash
# macOS / Linux
curl -fsSL https://dl.dagger.io/dagger/install.sh | sh

# Homebrew
brew install dagger

# Verify
dagger version
```

Requires a container runtime: Docker, Podman, nerdctl, or Apple Container.

## CLI commands

### Module lifecycle

```bash
dagger init --sdk=go --name=myproject     # create new module
dagger develop                             # regenerate SDK bindings
dagger functions                           # list available functions
dagger call <function> [flags]             # call a function
```

### Calling functions

```bash
# Simple call
dagger call build --src=.

# Chained calls (method on returned type)
dagger call build --src=. publish --address=ttl.sh/myapp:1h

# Export file to host
dagger call build --src=. export --path=./output/app

# Export directory
dagger call build-dir --src=. export --path=./dist/
```

### Passing arguments

```bash
# String / basic types
dagger call hello --name="world"

# Directories (local or remote)
dagger call build --src=.
dagger call build --src=https://github.com/user/repo

# Git refs
dagger call build --src=https://github.com/user/repo#branch

# Secrets from env vars
dagger call push --token=env:REGISTRY_TOKEN

# Secrets from files
dagger call push --token=file:./secret.txt

# Secrets from command output
dagger call push --token=cmd:"vault read -field=token secret/registry"
```

### Using remote modules directly

Run functions from Daggerverse modules without writing code:

```bash
# Use a Go builder module
dagger -m github.com/kpenfound/dagger-modules/golang@v0.2.1 \
    call build --source=. publish --address=ttl.sh/myapp:1h

# Use a community linter
dagger -m github.com/example/golangci-lint \
    call lint --source=.
```

### Module dependencies

```bash
# Install a dependency
dagger install github.com/kpenfound/dagger-modules/golang@v0.2.1

# Install a toolchain (no-code usage)
dagger toolchain install github.com/example/linter

# Show module info
dagger config
```

## Dagger Shell (interactive REPL)

Launch with `dagger` (no arguments):

```
$ dagger

# Create and manipulate containers
> container | from alpine | with-exec uname -a | stdout
> container | from golang:1.23 | terminal     # interactive shell

# Work with directories
> directory | with-new-file hello.txt "hi" | entries

# Context-sensitive help
> container | from alpine | .help
> container | from alpine | .help with-exec

# Chain complex workflows
> container | from alpine | \
    with-new-file /hi.txt "Hello!" | \
    with-entrypoint cat /hi.txt | \
    publish ttl.sh/hello

# Run module functions (if in a module directory)
> build --src=.
> test --src=.
```

### Shell tips

- Pipe (`|`) chains function calls on returned types
- `terminal` opens an interactive shell inside any container for debugging
- `.help` on any type shows available methods
- All function/arg names use kebab-case in Shell and CLI

## CI integration

### GitHub Actions (with dagger-for-github action)

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read

jobs:
  ci:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Run Code Quality Checks
        uses: dagger/dagger-for-github@v8
        with:
          verb: call
          args: checks
          cloud-token: ${{ secrets.DAGGER_CLOUD_TOKEN }}
          version: "latest"

      - name: Run Tests
        uses: dagger/dagger-for-github@v8
        with:
          verb: call
          args: test
          cloud-token: ${{ secrets.DAGGER_CLOUD_TOKEN }}
          version: "latest"
```

Pin action SHAs in production for supply-chain security:
```yaml
uses: dagger/dagger-for-github@456fc3af63a2ba6f9789af9c55045b459115541b # v8.3.0
uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2
```

Or with plain CLI (no GitHub Action):

```yaml
jobs:
  ci:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install Dagger
        run: curl -fsSL https://dl.dagger.io/dagger/install.sh | sh
      - name: Run pipeline
        run: dagger call ci --src=. --address=ttl.sh/myapp:${{ github.sha }}
```

For publish workflows with SBOM, provenance, and signing:
→ See [references/security-scanning.md](../references/security-scanning.md)

### GitLab CI

```yaml
.dagger:
  image: docker:latest
  services:
    - docker:dind
  before_script:
    - apk add curl
    - curl -fsSL https://dl.dagger.io/dagger/install.sh | sh

ci:
  extends: .dagger
  script:
    - dagger call ci --src=. --address=$CI_REGISTRY_IMAGE:$CI_COMMIT_SHA
```

## Debugging & observability

### Interactive debugging

Insert `terminal` at any point in a chain to inspect the container:

```go
// In code — opens a terminal for debugging
func (m *Myproject) Debug(src *dagger.Directory) *dagger.Container {
    return m.base(src).Terminal()
}
```

From Shell:
```
> container | from alpine | with-exec apk add curl | terminal
```

### Built-in TUI

The CLI shows a live terminal UI during execution with progress, timing,
and cache status for every operation.

### OpenTelemetry traces

Dagger emits OTel spans for every operation. Export to Jaeger, Honeycomb,
or any compatible backend:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
dagger call ci --src=.
```

### Dagger Cloud

Dagger Cloud provides a web UI for visualizing traces, drilling into logs,
and monitoring cache performance across local and CI runs:

```bash
export DAGGER_CLOUD_TOKEN=your-token
dagger call ci --src=.
```

## Module management

### dagger.json

The module config file tracks SDK, dependencies, and metadata:

```json
{
  "name": "myproject",
  "engineVersion": "v0.19.10",
  "sdk": {
    "source": "go"
  },
  "dependencies": [
    {
      "name": "golang",
      "source": "github.com/kpenfound/dagger-modules/golang@v0.2.1"
    }
  ]
}
```

### Go workspace integration

For modules inside a larger Go project, use Go workspaces:

```bash
# At repo root
go work init
go work use .
go work use ./.dagger
```

This gives your IDE full type resolution across both your app and Dagger code.
