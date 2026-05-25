# Claude Code Setup

Everything project-tracked for Claude Code lives under `.claude/`. There is **no `.mcp.json` at the repo root** by design — the svelte MCP server is registered per-developer via `make claude-bootstrap` (see below).

**Toolchain note:** this project uses **`bun` / `bunx`** for all JS-runtime tooling. Do **not** substitute `npm` / `npx` / `pnpm` / `pnpx` — they are not on PATH and the MCP install commands assume `bunx`.

## Bootstrap (fresh checkout)

```bash
make claude-bootstrap
```

What it does:

- Registers the svelte MCP server at **local** scope using `claude mcp add -t stdio -s local svelte -- bunx -y @sveltejs/mcp`. Idempotent — re-running is safe.
- Verifies `.claude/settings.json` is present.
- Prints the remaining manual steps.

**Where `-s local` actually stores it:** Claude Code's "local" scope writes to the **project-scoped section of `~/.claude.json`** (a per-developer file in your home directory, keyed by project path), NOT to a file inside this repo. That's the closest Claude Code lets us get to "team-shared, scriptable, but not a repo-root file" without using `.mcp.json`. The install command itself is tracked in the `Makefile` — that's the source of truth for "which MCP servers does this project need".

What it can't do (run these once in Claude Code itself):

```text
/plugin marketplace add spences10/svelte-skills-kit
/plugin marketplace add astral-sh/claude-code-plugins
/plugin marketplace add spences10/claude-code-toolkit
/plugin marketplace add denoland/skills
/plugin marketplace add redis/agent-skills
/plugin marketplace add sveltejs/ai-tools

/plugin install svelte-skills@svelte-skills-kit
/plugin install mcp-essentials@claude-code-toolkit
/plugin install analytics@claude-code-toolkit
/plugin install toolkit-skills@claude-code-toolkit
/plugin install deno-skills@denoland-skills
/plugin install redis-development@redis
/plugin install svelte@sveltejs-ai-tools
/plugin install astral@astral-sh
```

The set of enabled plugins is tracked in `.claude/settings.json` under `enabledPlugins`, so once any one developer has installed them, Claude Code can verify the state on other machines against that file.

## Layout

```
.claude/
├── README.md              # this file — source of truth
├── settings.json          # team-shared settings (committed): plugins, permissions, hooks
├── settings.local.json    # personal overrides (gitignored): includes the local-scope svelte MCP
├── commands/              # project-local slash commands
├── hooks/                 # project-local lifecycle hooks
└── skills/                # project-local skills (writing-python, python-infrastructure, fastapi, ...)
```

No `.mcp.json` at the repo root — by design. See "MCP servers" below.

## MCP servers

The svelte MCP is registered by `make claude-bootstrap` using `claude mcp add -s local`. To add another MCP server, follow the same pattern with `bunx` as the runtime:

```bash
claude mcp add -t stdio -s local <name> -- bunx -y <package>
```

The three scopes Claude Code supports for MCP:

| Scope | Stored in | Notes |
|---|---|---|
| `local` | Project-scoped section of `~/.claude.json` | Per-developer, this project only. **Used here.** Not a repo file. |
| `project` | `.mcp.json` at repo root | Team-shared, committed. **Not used here** — we explicitly avoid root-level files. |
| `user` | Global section of `~/.claude.json` | Per-developer, all projects. Use for personal MCPs unrelated to any single repo. |

The install command in `Makefile`'s `claude-bootstrap` target is the source of truth — that's where you can see "which MCP servers does this project need". The actual config rows land in `~/.claude.json`, which is per-developer and not in the repo at all. If a new MCP should be required for everyone, append another `claude mcp add` line to that target.

## Install Library-Bundled Skills

Some libraries (e.g. FastAPI, Streamlit) ship their own agent skills inside the published package, kept in sync with each release. Use [`library-skills`](https://library-skills.io) to discover what your installed dependencies expose and symlink them into this project so they always match the installed version.

Run from the project root (the one with `pyproject.toml` / `package.json`):

```bash
# Python (uv)
uvx library-skills --claude

# Node.js / Bun
bunx library-skills --claude
```

`--claude` is required: by default the CLI installs to `.agents/skills/` (the cross-agent standard), and Claude Code only reads `.claude/skills/`. Use `--copy` instead of symlinks on Windows or filesystems without symlink support.

What it does:

1. Reads the direct dependencies in `pyproject.toml` / `package.json`.
2. Scans `.venv/.../site-packages/<pkg>/.agents/skills/*/SKILL.md` and `node_modules/<pkg>/.agents/skills/*/SKILL.md` for bundled skills.
3. Prompts you to pick which to install; creates symlinks in `.claude/skills/` so updating the library updates the skill.

Useful flags: `--all` (install every discovered skill non-interactively), `-s <name>` (install a specific skill, including transitive deps), `--check` (CI mode, exit 1 if installed skills drift from the library).

## Marketplaces

| Marketplace | Repo | Plugins |
|---|---|---|
| svelte-skills-kit | [spences10/svelte-skills-kit](https://github.com/spences10/svelte-skills-kit) | svelte-skills (runes, SvelteKit data flow, components, deployment) |
| claude-code-toolkit | [spences10/claude-code-toolkit](https://github.com/spences10/claude-code-toolkit) | mcp-essentials, analytics, toolkit-skills |
| denoland-skills | [denoland/skills](https://github.com/denoland/skills) | deno-skills |
| redis | [redis/agent-skills](https://github.com/redis/agent-skills) | redis-development |
| sveltejs-ai-tools | [sveltejs/ai-tools](https://github.com/sveltejs/ai-tools) | svelte |

## Activation hook (recommended)

Skills don't auto-activate reliably without a hook. The forced-eval hook gets 84% activation vs 20% without:

```bash
bunx claude-skills-cli add-hook
```

## Creating Skills

Use [claude-skills-cli](https://github.com/spences10/claude-skills-cli) for scaffolding and validation:

```bash
# Create a new skill
bunx claude-skills-cli init --name my-skill --description "Brief description"

# Validate
bunx claude-skills-cli validate .claude/skills/my-skill

# Stats for all skills
bunx claude-skills-cli stats .claude/skills
```

Skills load in 3 levels (progressive disclosure):

| Level | Content | When Loaded | Size Limit |
|---|---|---|---|
| 1 | SKILL.md metadata (YAML) | Always in context | <200 chars |
| 2 | SKILL.md body (Markdown) | When skill triggers | ~50 lines |
| 3 | references/, scripts/, assets/ | As needed | Unlimited |

## Troubleshooting

- **Svelte MCP not loading?** Re-run `make claude-bootstrap`. The install is idempotent — it'll print "already installed" if it's there, or add it if it's missing. Verify with `claude mcp list`.
- **Skill not activating?** See "Activation hook" above. Also check `.claude/skills/<name>/SKILL.md` has a valid `description` with a clear trigger.
- **Settings drift?** `.claude/settings.json` is the source of truth for `enabledPlugins`; `.claude/settings.local.json` is for personal overrides only (including the local-scope MCP server).
