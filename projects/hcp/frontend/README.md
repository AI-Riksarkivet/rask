# HCP Frontend

SvelteKit 2 + Svelte 5 frontend for the HCP application, running on **Deno**.

## Tech Stack

- **Runtime:** Deno
- **Framework:** SvelteKit 2 + Svelte 5
- **Styling:** Tailwind CSS 4, tw-animate-css
- **Components:** Bits UI, Lucide Svelte icons
- **Forms:** SvelteKit Superforms + Zod validation
- **Theme:** Mode Watcher (light/dark toggle)
- **Animations:** GSAP

## Getting Started

### Install dependencies

```bash
deno install
```

### Start the dev server

```bash
deno task dev
```

Or via the root Makefile (also sets `BACKEND_URL`):

```bash
make frontend-dev
```

The dev server starts at `http://localhost:5173` and proxies API calls to the
backend at `http://localhost:8000`.

### Build for production

```bash
deno task build
```

### Preview the production build

```bash
deno task preview
```

## Available Tasks

| Task      | Command             | Description              |
| --------- | ------------------- | ------------------------ |
| `dev`     | `deno task dev`     | Start Vite dev server    |
| `build`   | `deno task build`   | Build for production     |
| `preview` | `deno task preview` | Preview production build |
| `sync`    | `deno task sync`    | SvelteKit sync (codegen) |
| `check`   | `deno task check`   | TypeScript type checking |
| `fmt`     | `deno task fmt`     | Format code (Deno fmt)   |
| `lint`    | `deno task lint`    | Lint code (Deno lint)    |

## Project Structure

```
frontend/
├── src/
│   ├── routes/
│   │   ├── login/              # Authentication
│   │   ├── (app)/              # Protected routes
│   │   │   ├── dashboard/      # Main dashboard
│   │   │   ├── tenants/        # Tenant management
│   │   │   ├── buckets/        # S3 bucket browser
│   │   │   └── users/          # User management
│   │   └── api/[...path]/      # API proxy
│   └── lib/
│       ├── components/         # Shared components
│       └── hooks/              # Svelte hooks
├── deno.json                   # Deno tasks and config
├── package.json                # npm dependencies
├── svelte.config.js            # Svelte compiler config
├── vite.config.ts              # Vite bundler config
└── tsconfig.json               # TypeScript config
```
