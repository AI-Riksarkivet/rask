---
title: ra-hcp
template: home.html
hide:
  - navigation
  - toc
  - path
---

[![Tests](https://github.com/AI-Riksarkivet/ra-hcp/actions/workflows/ci.yml/badge.svg)](https://github.com/AI-Riksarkivet/ra-hcp/actions/workflows/ci.yml)
[![Docs](https://github.com/AI-Riksarkivet/ra-hcp/actions/workflows/docs.yml/badge.svg)](https://github.com/AI-Riksarkivet/ra-hcp/actions/workflows/docs.yml)
[![Storybook](https://github.com/AI-Riksarkivet/ra-hcp/actions/workflows/storybook.yml/badge.svg)](https://github.com/AI-Riksarkivet/ra-hcp/actions/workflows/storybook.yml)
[![CodeQL](https://github.com/AI-Riksarkivet/ra-hcp/actions/workflows/codeql.yml/badge.svg)](https://github.com/AI-Riksarkivet/ra-hcp/actions/workflows/codeql.yml)
[![Scorecard](https://github.com/AI-Riksarkivet/ra-hcp/actions/workflows/scorecard.yml/badge.svg)](https://github.com/AI-Riksarkivet/ra-hcp/actions/workflows/scorecard.yml)

<div class="home-grid" markdown>

## Features

<div class="grid cards" markdown>

-   :lucide-hard-drive:{ .lg .middle } **S3 Object Storage**

    ---

    Presigned URL transfers, multipart uploads, bulk operations with
    crash-safe resume, and staging/commit patterns for atomic writes.

    [:octicons-arrow-right-24: S3 API](api/s3-objects.md)

-   :lucide-settings:{ .lg .middle } **Management API**

    ---

    Full HCP MAPI coverage — tenants, namespaces, users, groups,
    compliance, replication, and system administration.

    [:octicons-arrow-right-24: MAPI guide](api/tenants.md)

-   :lucide-terminal:{ .lg .middle } **Python SDK & CLI**

    ---

    Async client with automatic retries, batch presigning, IIIF image
    downloads, and a Typer CLI for scripting.

    [:octicons-arrow-right-24: SDK docs](sdk/)

-   :lucide-layout-dashboard:{ .lg .middle } **Web Dashboard**

    ---

    SvelteKit 2 frontend for browsing buckets, managing namespaces,
    monitoring storage, and administering users.

    [:octicons-arrow-right-24: Architecture](architecture/)

</div>

## Quick start

=== "Development (mock)"

    ```bash
    make setup          # Install Deno + uv and all dependencies
    make run-api-mock   # Start mock backend (no HCP credentials needed)
    make frontend-dev   # Start frontend dev server
    ```

=== "Production (real HCP)"

    ```bash
    make setup          # Install Deno + uv and all dependencies
    make run-api        # Start backend with real HCP credentials
    make frontend-dev   # Start frontend dev server
    ```

=== "Python SDK"

    ```bash
    uv pip install rahcp

    rahcp auth whoami
    rahcp s3 ls
    rahcp s3 upload-all my-bucket ./data --workers 20
    ```

## Tech stack

| Layer | Technology |
|-------|------------|
| **Frontend** | SvelteKit 2, Svelte 5, Tailwind CSS 4, Deno |
| **Backend** | FastAPI, boto3, Python 3.13, uv |
| **SDK** | rahcp-client, rahcp-cli, rahcp-tracker, rahcp-iiif |
| **Caching** | Redis |
| **Docs** | Zensical |
| **CI/CD** | GitHub Actions, Docker, Helm |

</div>
