# Claude Code Setup

Everything project-tracked for Claude Code lives under `.claude/`. There is **no `.mcp.json` at the repo root** by design — the svelte MCP server is registered per-developer via `make claude-bootstrap` (see below).

**Toolchain note:** this project uses **`bun` / `bunx`** for all JS-runtime tooling. Do **not** substitute `npm` / `npx` / `pnpm` / `pnpx` — they are not on PATH and the MCP install command assumes `bunx`.

## Skills come from the `ra-skills` marketplace — not this repo

rask **does not vendor skills** under `.claude/skills/` anymore. All shared skills live in **[`AI-Riksarkivet/ra-skills`](https://github.com/AI-Riksarkivet/ra-skills)**, the single source of truth, and are consumed as a Claude Code marketplace. This kills the per-repo copy-drift that used to happen when every project carried its own diverging copy of `writing-python`, `dagger`, `fastapi`, …

- **CORE** (language/toolchain): `writing-python`, `writing-typescript`, `fastapi`, `testing-python`, `python-infrastructure`, `otel`, `dagger`, `dockerfile`, `turborepo`, `zensical-setup`, `zensical-authoring`.
- **PROJECT** (`rask-*`): `rask-architecture`, `rask-services-fleet`, `rask-htr-pipeline`, `rask-orchestrator`.

To **change a skill, edit it in ra-skills** and re-run `make claude-bootstrap` here. The full RA Claude surface (skills + third-party marketplaces + MCP servers) is documented once, canonically, in **[ra-skills' README](https://github.com/AI-Riksarkivet/ra-skills#what-we-use--the-full-ra-claude-surface)** — this file only covers rask-specific setup.

## Bootstrap (fresh checkout)

```bash
make claude-bootstrap
```

What it does (all idempotent — re-run anytime):

1. Registers the **svelte MCP** server at **local** scope (`claude mcp add -t stdio -s local svelte -- bunx -y @sveltejs/mcp`).
2. Adds every marketplace declared in `.claude/settings.json` → `extraKnownMarketplaces` (`claude plugin marketplace add <repo>`).
3. Installs every plugin in `.claude/settings.json` → `enabledPlugins` at **project** scope (`claude plugin install <name>@<marketplace> -s project`).

`.claude/settings.json` is the **single source of truth** — the bootstrap is driven from it, so a fresh checkout reproduces the exact active skill surface. Don't hand-curate plugins outside it.

**Where the svelte MCP `-s local` actually stores it:** Claude Code's "local" scope writes to the **project-scoped section of `~/.claude.json`** (a per-developer file keyed by project path), NOT a file inside this repo. That's the closest we get to "team-shared, scriptable, not a repo-root file" without a root `.mcp.json`. The `claude mcp add` line in the `Makefile` is the source of truth for which MCP servers this project needs.

## The active surface (summary)

| Kind | What | Where it's declared |
|---|---|---|
| **RA-owned skills** | the 15 `ra-skills` plugins above | `enabledPlugins` (`*@ra-skills`) + `extraKnownMarketplaces.ra-skills` |
| **3rd-party plugins** | `toolkit-skills` / `mcp-essentials` / `analytics` (claude-code-toolkit), `astral`, `svelte-skills`, `redis-development` (claude-plugins-official), `hf-cli` / `huggingface-trackio` (huggingface-skills) | `enabledPlugins` + `extraKnownMarketplaces` |
| **MCP server** | `svelte` (Svelte 5 MCP, `@sveltejs/mcp`) | `make claude-bootstrap` (local scope) |

> Editor support: ra-skills targets the **Claude ecosystem** — Claude Code in the terminal, **VS Code**, and **Zed** (which also read the generated `AGENTS.md`). No Gemini / Codex / Cursor.

## Layout

```
.claude/
├── README.md              # this file — rask-specific Claude setup
├── settings.json          # committed: enabledPlugins, extraKnownMarketplaces, (permissions/hooks)
├── settings.local.json    # personal overrides (gitignored): includes the local-scope svelte MCP
├── commands/              # project-local slash commands
└── hooks/                 # project-local lifecycle hooks
# no skills/ — skills come from the ra-skills marketplace
```

## MCP servers

The svelte MCP is registered by `make claude-bootstrap`. To add another, follow the same pattern with `bunx` as the runtime:

```bash
claude mcp add -t stdio -s local <name> -- bunx -y <package>
```

| Scope     | Stored in                                  | Notes                                                                            |
| --------- | ------------------------------------------ | -------------------------------------------------------------------------------- |
| `local`   | Project-scoped section of `~/.claude.json` | Per-developer, this project only. **Used here.** Not a repo file.                |
| `project` | `.mcp.json` at repo root                   | Team-shared, committed. **Not used here** — we avoid root-level files.           |
| `user`    | Global section of `~/.claude.json`         | Per-developer, all projects. Use for personal MCPs unrelated to any single repo. |

## Troubleshooting

- **Skill not available?** Run `make claude-bootstrap` — it adds the marketplaces and installs the enabled plugins. Verify with `claude plugin list` and `claude plugin marketplace list`. To update skills after a change lands in ra-skills: `claude plugin update <name>@ra-skills` (or re-run bootstrap).
- **Svelte MCP not loading?** Re-run `make claude-bootstrap` (idempotent). Verify with `claude mcp list`.
- **Settings drift?** `.claude/settings.json` is the source of truth for `enabledPlugins` + `extraKnownMarketplaces`; `.claude/settings.local.json` is personal overrides only (including the local-scope MCP).
