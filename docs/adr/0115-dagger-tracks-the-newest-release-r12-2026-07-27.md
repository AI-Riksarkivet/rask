# 0115. Dagger tracks the newest release (R12, 2026-07-27)

Source: `docs/architecture/lance-ns-merge.md:447` (at `44b354f3`) (owner ruling R12, 2026-07-27, wave 2; supersedes the same-day v0.20.3 hold).

## Context

The first copy wave held Dagger at v0.20.3, because the copied module had to prove itself at the pins before
any version moved. The merged `.dagger` module combines rask's functions with lance-ns's (charts, checks, e2e,
frontend, openapi).

## Decision

Dagger tracks the newest release. The CLI, `dagger.json`'s `engineVersion` and the regenerated SDK bindings
move together, and the merged module must compile and list its functions at the new version.

## Consequences

- `dagger.json:3` (repo root) pins `engineVersion` `v0.21.7`. Whether that is the newest release on any given day
  is not something the tree can show; a CLI upgrade without re-running `make dagger-engine` makes the CLI
  provision its own config-less engine (`CLAUDE.md`, the in-cluster loop).
- Dagger is the only image driver; this ruling is about its version, and the no-docker rule in `CLAUDE.md`
  stands independently of it.
