# Claude Code Setup

**Shared** language/toolchain skills come from the **[`ra-skills`](https://github.com/AI-Riksarkivet/ra-skills)** marketplace — not vendored here, so there's one canonical copy and no cross-repo drift.

rask's **own project skills** live in `.claude/skills/` — they describe rask's internals and evolve with the code, so they stay in this repo (the same way ra-hcp keeps its `hcp-*` skills local):

| Skill | Covers |
| --- | --- |
| `rask-architecture` | Workspace planes, globbed membership, the `make_service_app` seam, deployables |
| `rask-services-fleet` | Gateway routing, ports, `RASK_*_URL`, the 404/502/403 contracts |
| `rask-frontend` | The 7 zones, three data dialects, MFE composition, the gates |
| `rask-styling` | `@rask/ui` — OKLCH tokens, Tailwind 4 `@source`, component authoring |
| `rask-htr-pipeline` | `runners/htr` Ray Data + Ray Serve GPU packing and the OOM lessons |
| `openfga` | Authorization modelling (a vendored copy of OpenFGA's upstream skill, v1.2.1 — edits fork it) |

**Keeping them true:** every claim in these skills is meant to be traceable to a file. When code moves and a skill contradicts it, fix the skill in the same commit. A claim that is checkable should become a gate (`@rask/zone-contract`, an oxlint rule, a test) rather than prose.

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
