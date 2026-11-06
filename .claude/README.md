# Claude Code Setup

**Shared** language/toolchain skills come from the **[`ra-skills`](https://github.com/AI-Riksarkivet/ra-skills)** marketplace — not vendored here, so there's one canonical copy and no cross-repo drift.

rask's **own project skills** live in `.claude/skills/` — they describe rask's internals and evolve with the code, so they stay in this repo (the same way ra-hcp keeps its `hcp-*` skills local):

| Skill | Covers |
| --- | --- |
| `rask-architecture` | Workspace planes, globbed membership, the `make_service_app` seam, deployables |
| `rask-services-fleet` | Gateway routing, ports, `RASK_*_URL`, the 404/502/403 contracts |
| `rask-frontend` | The console SPA: route tree, browser PKCE sign-in, data layer, gates |
| `rask-styling` | `@rask/ui` — OKLCH tokens, Tailwind 4 `@source`, component authoring |
| `rask-lance-catalog` | The Lance Namespace spec, the catalog's hierarchy, guards, registries, protection/trash, maintenance |
| `rask-notifications` | The targeting contract the lakehouse's events publish, and how the notifications inbox consumes it |
| `rask-dapr` | Which Dapr building blocks the estate uses or refused, and the component-vs-DIY rubric |
| `rask-rustfs` | RustFS as the dev/CI object store: its zero-trust model, S3 behaviour, provisioning, traps |
| `rask-testing` | rask's pytest wiring, its real-collaborator fixtures, the row rule for adding tests |
| `rask-helm` | The product chart and its release: the 1 MiB ceiling, upgrade traps, the zero-trust rule |
| `openfga` | Authorization modelling (forked from OpenFGA's upstream skill v1.2.1: Python SDK only, with rask's model paths) |

**Keeping them true:** every claim in these skills is meant to be traceable to a file. When code moves and a skill contradicts it, fix the skill in the same commit. A claim that is checkable should become a gate (an import-linter contract, a ruff or oxlint rule, a lint script such as `scripts/lakehouse_names_no_client.py`, a behaviour test) rather than prose.

The full RA Claude surface lives in [ra-skills' README](https://github.com/AI-Riksarkivet/ra-skills#what-we-use--the-full-ra-claude-surface).

## Setup

```bash
make claude-bootstrap   # idempotent — re-run anytime
```

Driven entirely from `.claude/settings.json` (the single source of truth): it registers the svelte MCP, adds the declared marketplaces, and installs the enabled plugins at project scope. To change a skill, edit it in **ra-skills**, then re-run this (or `claude plugin update <name>@ra-skills`).

> **No root `.mcp.json` by design** — the svelte MCP is registered at `local` scope by the `claude mcp add` line in the `Makefile`.
> **Toolchain:** Bun only (`bun` / `bunx`), never `npm` / `pnpm`.

## Troubleshooting

- **Skill missing?** Run `make claude-bootstrap`, then check `claude plugin list`.
- **Drift?** `.claude/settings.json` is authoritative; `.claude/settings.local.json` is personal-only.
