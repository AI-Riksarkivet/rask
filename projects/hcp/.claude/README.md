# Claude Code Setup

## Install Skills

### 1. Add marketplaces (one-time)

```
/plugin marketplace add spences10/svelte-skills-kit
/plugin marketplace add astral-sh/claude-code-plugins
/plugin marketplace add spences10/claude-code-toolkit
/plugin marketplace add denoland/skills
/plugin marketplace add redis/agent-skills
/plugin marketplace add sveltejs/ai-tools
```

### 2. Install plugins

```
/plugin install svelte-skills@svelte-skills-kit
/plugin install mcp-essentials@claude-code-toolkit
/plugin install analytics@claude-code-toolkit
/plugin install toolkit-skills@claude-code-toolkit
/plugin install deno-skills@denoland-skills
/plugin install redis-development@redis
/plugin install svelte@sveltejs-ai-tools
/plugin install astral@astral-sh

```

These are tracked in `settings.json`:

```json
{
  "enabledPlugins": {
   ...
  }
}
```

### 3. Add activation hook (recommended)

Skills don't auto-activate reliably without a hook. The forced-eval hook gets 84% activation vs 20% without:

```bash
pnpx claude-skills-cli add-hook
```

## Marketplaces

| Marketplace | Repo | Plugins |
|---|---|---|
| svelte-skills-kit | [spences10/svelte-skills-kit](https://github.com/spences10/svelte-skills-kit) | svelte-skills (runes, SvelteKit data flow, components, deployment) |
| claude-code-toolkit | [spences10/claude-code-toolkit](https://github.com/spences10/claude-code-toolkit) | mcp-essentials, analytics, toolkit-skills |
| denoland-skills | [denoland/skills](https://github.com/denoland/skills) | deno-skills |
| redis | [redis/agent-skills](https://github.com/redis/agent-skills) | redis-development |
| sveltejs-ai-tools | [sveltejs/ai-tools](https://github.com/sveltejs/ai-tools) | svelte |

## Creating Skills

Use [claude-skills-cli](https://github.com/spences10/claude-skills-cli) for scaffolding and validation:

```bash
# Create a new skill
pnpx claude-skills-cli init --name my-skill --description "Brief description"

# Validate
pnpx claude-skills-cli validate .claude/skills/my-skill

# Stats for all skills
pnpx claude-skills-cli stats .claude/skills
```

Skills load in 3 levels (progressive disclosure):

| Level | Content | When Loaded | Size Limit |
|---|---|---|---|
| 1 | SKILL.md metadata (YAML) | Always in context | <200 chars |
| 2 | SKILL.md body (Markdown) | When skill triggers | ~50 lines |
| 3 | references/, scripts/, assets/ | As needed | Unlimited |
