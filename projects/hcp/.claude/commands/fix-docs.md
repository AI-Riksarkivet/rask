Synchronize the Zensical documentation with the current API source code.

**GOAL:** Ensure docs/ accurately reflects the actual FastAPI endpoints, schemas, services, and architecture in backend/.

**STEPS:**

1. **Read the current API source** — scan all files in these directories:
   - `backend/app/api/v1/endpoints/` (all endpoint modules)
   - `backend/app/api/dependencies.py` (DI wiring, storage factory)
   - `backend/app/schemas/` (all Pydantic models)
   - `backend/app/services/mapi_service.py` (MapiService)
   - `backend/app/services/storage/` (StorageProtocol, adapters, factory)
   - `backend/app/services/cached_storage.py` (CachedStorage)
   - `backend/app/services/cache_service.py` (CacheService)
   - `backend/app/services/query_service.py` (QueryService)
   - `backend/app/core/config.py` (environment variables)
   - `backend/app/core/security.py` (auth mechanism)
   - `backend/app/core/telemetry.py` (OTel setup)
   - `backend/app/main.py` (app setup, tags, middleware)

2. **Read the current docs** — read all markdown files under `docs/`.

3. **Compare and identify drift:**
   - New endpoints not documented
   - Removed endpoints still documented
   - Changed request/response schemas (fields added, removed, renamed)
   - Changed environment variables or configuration
   - Changed authentication flow
   - Architecture diagrams that don't match actual class structure

4. **Update the docs** to match the source code:
   - `docs/getting-started/index.md` — quick start, run commands
   - `docs/getting-started/configuration.md` — environment variables table (HCP, S3, Redis, OTel, Auth, App)
   - `docs/api/authentication.md` — auth flow, JWT details
   - `docs/api/s3-buckets.md` — bucket endpoints (endpoint table + narrative only, link to Swagger for field details)
   - `docs/api/s3-objects.md` — object endpoints (endpoint table + narrative only)
   - `docs/api/tenants.md` — tenant management endpoints
   - `docs/api/namespaces.md` — namespace management endpoints
   - `docs/api/system.md` — system-level endpoints
   - `docs/architecture/backend.md` — backend layers diagram, design decisions
   - `docs/architecture/storage.md` — storage layer diagram, adapters, @delegates_to pattern
   - `docs/architecture/frontend.md` — frontend data flow, RBAC

5. **Update navigation** — if new doc pages are needed, update `zensical.toml` nav.

**FORMAT RULES:**
- Use Zensical/MkDocs markdown (admonitions, code blocks, tables)
- API guide pages should have: description, endpoint table (method, path, description), and narrative workflows — do NOT duplicate field-by-field schema docs (those are auto-generated in the API Reference section via mkdocstrings)
- Keep content concise — link to Swagger UI (`/docs`) and auto-generated reference pages for full details
- Use `!!! tip` or `!!! warning` admonitions for important notes

**IMPORTANT:**
- Only update docs that are actually out of date — do not rewrite pages that are already correct
- Do not remove content that is manually authored and still valid
- Architecture diagrams should match the actual class names, file paths, and relationships in the code
- Report a summary of what changed when done
