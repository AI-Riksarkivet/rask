# Claude Code Setup

**Shared** language/toolchain skills come from the **[`ra-skills`](https://github.com/AI-Riksarkivet/ra-skills)** marketplace — not vendored here, so there's one canonical copy and no cross-repo drift.

rask's **own project skills** (`rask-architecture`, `rask-services-fleet`, `rask-htr-pipeline`, `rask-orchestrator`) live in `.claude/skills/` — they describe rask's internals and evolve with the code, so they stay in this repo (the same way ra-hcp keeps its `hcp-*` skills local).

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
