---
title: OpenFGA CLI Usage
---

## OpenFGA CLI Usage

Use the OpenFGA CLI to validate and test your models.

**MANDATORY**: Always run `fga model test` after creating or modifying any `.fga` or `.fga.yaml` file. Do not consider any OpenFGA task complete until tests pass.

Use the OpenFGA CLI to validate and test your models.

**Installation (rask):** `make bootstrap` pins the `fga` CLI into `.localbin/fga` (the Makefile's `FGA_V`), and
`make fga-test` runs the model's own suite with it. The OpenFGA server the offline suite starts is exported from its
pinned image through Dagger by `make openfga` (`.localbin/openfga`). Never run either through docker (CLAUDE.md
toolchain rule).

```bash
make bootstrap   # .localbin/fga, idempotent
make fga-test    # validate model.fga, run model.fga.yaml, check the three copies agree
```

**Validate model syntax:**

```bash
fga model validate --file model.fga
```

**Run tests:**

```bash
fga model test --tests store.fga.yaml
```

**Transform between formats:**

```bash
# DSL to JSON
fga model transform --input model.fga --output model.json

# JSON to DSL
fga model transform --input model.json --output model.fga
```

**Example test run:**

```bash
$ fga model test --tests store.fga.yaml
# Test Summary #
Tests 1/1 passing
Checks 5/5 passing
```

**CI/CD integration:**

```bash
# Fail the build if tests don't pass
fga model test --tests store.fga.yaml || exit 1
```

You can also use the [OpenFGA Model Test GitHub actions](https://github.com/marketplace/actions/openfga-model-testing-action).

**Verbose output for debugging:**

```bash
fga model test --tests store.fga.yaml --verbose
```
