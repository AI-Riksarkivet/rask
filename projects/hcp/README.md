# ra-hcp

[![Tests](https://github.com/AI-Riksarkivet/ra-hcp/actions/workflows/ci.yml/badge.svg)](https://github.com/AI-Riksarkivet/ra-hcp/actions/workflows/ci.yml)
[![Docs](https://github.com/AI-Riksarkivet/ra-hcp/actions/workflows/docs.yml/badge.svg)](https://github.com/AI-Riksarkivet/ra-hcp/actions/workflows/docs.yml)
[![Storybook](https://github.com/AI-Riksarkivet/ra-hcp/actions/workflows/storybook.yml/badge.svg)](https://github.com/AI-Riksarkivet/ra-hcp/actions/workflows/storybook.yml)
[![CodeQL](https://github.com/AI-Riksarkivet/ra-hcp/actions/workflows/codeql.yml/badge.svg)](https://github.com/AI-Riksarkivet/ra-hcp/actions/workflows/codeql.yml)
[![Scorecard](https://github.com/AI-Riksarkivet/ra-hcp/actions/workflows/scorecard.yml/badge.svg)](https://github.com/AI-Riksarkivet/ra-hcp/actions/workflows/scorecard.yml)
[![TruffleHog](https://github.com/AI-Riksarkivet/ra-hcp/actions/workflows/trufflehog.yml/badge.svg)](https://github.com/AI-Riksarkivet/ra-hcp/actions/workflows/trufflehog.yml)

Web application for managing Hitachi Content Platform (HCP) — S3-compatible object storage and Management API operations.

| Layer | Technology |
|-------|------------|
| Frontend | SvelteKit 2, Svelte 5, Tailwind CSS 4, Deno |
| Backend | FastAPI, boto3, Python, uv |
| Caching | Redis |
| Docs | Zensical |
| CI/CD | Dagger, Docker, Helm |

## Quick Start

```bash
make setup          # Install Deno + uv and all dependencies
make run-api-mock   # Start mock backend (no HCP credentials needed)
make frontend-dev   # Start frontend dev server
```

For production mode with real HCP credentials: `make run-api`

## Documentation

- [Docs site](https://ai-riksarkivet.github.io/hcp/) — architecture, API guide, configuration
- [Storybook](https://ai-riksarkivet.github.io/hcp/storybook/) — component library

## Requirements

- [Deno](https://deno.com) — frontend runtime
- [uv](https://docs.astral.sh/uv/) — Python package manager
- Docker — for Redis caching (optional)
