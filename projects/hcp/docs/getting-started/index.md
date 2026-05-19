# Getting Started

This guide walks you through setting up and running the HCP management application locally.

## Prerequisites

Before you begin, ensure the following tools are installed:

| Tool | Purpose | Install |
|------|---------|---------|
| [Deno](https://deno.land/) | Frontend runtime (SvelteKit) | `curl -fsSL https://deno.land/install.sh \| sh` |
| [uv](https://docs.astral.sh/uv/) | Backend Python package manager | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| [Docker](https://www.docker.com/) | Redis cache (optional) | [docker.com/get-docker](https://www.docker.com/get-docker) |

!!! tip
    Docker is only required if you want to enable Redis caching. The application runs without it.

## Installation

Install all dependencies (both frontend and backend) in one step:

```bash
make setup
```

Or install them individually:

```bash
make install-deno    # Install Deno
make install-uv      # Install uv and Python dependencies
```

## Start the Backend

The backend is a FastAPI application. You can run it in three modes depending on your needs.

### Production Mode

Connects to a real HCP system. Requires HCP credentials configured via environment variables (see [Configuration](configuration.md)).

```bash
make run-api
```

!!! warning
    Production mode requires valid HCP credentials. Set `HCP_HOST`, `HCP_USERNAME`, and `HCP_PASSWORD` in your `.env` file before starting. See the [Configuration](configuration.md) page for all available variables.

### Mock Server

Runs with a built-in mock server that simulates HCP responses. No credentials or HCP connectivity needed.

```bash
make run-api-mock
```

!!! tip
    The mock server is the easiest way to get started. Log in with username `admin` and password `password`.

## Start the Frontend

Start the frontend dev server:

```bash
make frontend-dev
```

!!! tip
    Frontend dependencies are installed automatically by `make setup`. If you skipped setup, run `cd frontend && deno install` first.

## Redis Cache (Optional)

Redis provides response caching to reduce load on the HCP system. It is entirely optional.

```bash
docker compose -f .docker/docker-compose.yml up redis -d   # Start Redis
docker compose -f .docker/docker-compose.yml down redis     # Stop Redis
```

!!! tip
    To enable caching, start Redis and set `REDIS_URL=redis://localhost:6379` in your `.env` file. When `REDIS_URL` is empty (the default), caching is disabled and the app queries HCP directly on every request.

## Access the Application

Once both the backend and frontend are running:

| Service | URL |
|---------|-----|
| Frontend (SvelteKit) | [http://localhost:5173](http://localhost:5173) |
| API documentation (Swagger) | [http://localhost:8000/docs](http://localhost:8000/docs) |

## Next Steps

- [Configuration](configuration.md) -- Environment variables reference for connecting to HCP, Redis, and tuning cache TTLs.
