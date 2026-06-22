# rask — "Upload images" view under Batches

**Date:** 2026-06-22
**Status:** Approved design
**Depends on:** the S3-source ingestion path (`register_volume` + `RASK_SOURCE_MODE=s3`) and the Docker Compose fleet (gateway + ingress + core-api + MinIO).

## Goal

Let a user ingest a new image volume from the browser: drop/select images under a
volume name, and have them uploaded into MinIO and registered as a `batches` row —
so the volume immediately appears in **Batches** (as `cached`, with `page_count`),
ready for the existing HTR trigger. No CLI, no manual `mc` upload.

## Decisions (from brainstorming)

- **Scope = upload + register.** One ingest action uploads the files and registers
  the volume. HTR transcription stays the existing separate trigger (the runner);
  auto-starting HTR is out of scope (the orchestrator's compose submission is a
  known, unrelated gap).
- **Upload goes through the backend, not the browser→MinIO.** Presigned URLs were
  considered and rejected: they require exposing MinIO's `:9000` to the browser
  (published port + CORS) and signing for a browser-reachable host. Streaming
  through our own API keeps MinIO internal — no published `:9000`, no CORS, no
  presigning.
- **Combined endpoint on core-api.** `POST /api/batches/{id}/upload` writes the
  files and then calls `register_volume`, returning the `BatchPublic`. One
  round-trip. core-api already owns batches + register + an internal S3 client.
- **Dedicated route `/batches/new`** (not an inline panel), linked from the
  Batches list page.

## Architecture

Browser (drop zone) → `FormData` POST → ingress (`:8888`) → gateway (`/api/*`) →
core-api `POST /api/batches/{id}/upload` → writes each file to
`images-batch/<id>/<filename>` via the internal S3 client (`http://minio:9000`) →
`register_volume(...)` → returns `BatchPublic` (201). The new volume appears in
Batches; pages render through the existing volumes-api.

### 1. Backend — `POST /api/batches/{id}/upload` (core-api)

- Location: `components/services/core/src/core/api/v1/endpoints/batches.py`, next to
  the existing `register` endpoint. Signature mirrors it (`SessionDep`,
  `SettingsDep`, `S3Dep`) plus `files: list[UploadFile]`.
- Per file:
  - **Filename safety:** reduce to `Path(file.filename).name` (basename only); reject
    empty names. This prevents `../` path traversal into other prefixes/buckets.
  - **Type filter:** keep only image suffixes (`.jpg`, `.jpeg`, `.png`, `.tif`,
    `.tiff`, case-insensitive). If *no* file is a valid image → `422` with a clear
    message. Non-image files in a mixed selection are skipped (reported back), not
    fatal.
  - **Write:** put the bytes to `images-batch/<id>/<basename>` using the same S3
    client/bucket (`settings.cache_bucket`) that `register_volume` reads. Reuse the
    storage layer (`storage.S3Client` / an `S3Sink`-style put) — no new S3 code if a
    put helper already exists; otherwise a thin `put_object` call.
- **Volume id safety:** validate `{id}` against `^[A-Za-z0-9_-]+$` (letters, digits,
  `-`, `_` only — no dots, so `..`/slashes/spaces are all rejected) → `422` on
  violation. (register has the same exposure via path; this
  endpoint adds the guard and register continues to receive a clean id.)
- After writes: call `register_volume(session, s3, input_bucket=settings.cache_bucket,
  volume_id=id)` and return its `BatchPublic` with `201`.
- **Idempotent:** re-uploading to an existing volume overwrites same-named objects,
  adds new ones, and re-registers (refreshes `page_count`/`cached_pages`,
  preserves `chunk_id`) — matching `register_volume`'s existing contract.

### 2. API client — `@rask/api`

- `uploadVolume(id: string, files: File[]): Promise<Batch>` — builds a `FormData`
  (`files` field repeated), client-side `fetch('/api/batches/<id>/upload', { method:
  'POST', body: form })`, returns the parsed `Batch`. Same-origin (ingress proxies
  `/api`), so no CORS and no `GATEWAY_URL` needed (client-side path, like the other
  `@rask/api` mutations).
- `registerVolume(id: string): Promise<Batch>` — POST `/api/batches/<id>/register`,
  exposed for completeness (the UI currently has neither).
- Types reuse the existing `Batch` shape from `@rask/api`.

### 3. Frontend — `/batches/new`

- New route `components/apps/frontend/src/routes/batches/new/+page.svelte`.
- UI (shadcn-svelte + `@rask/ui`, matching existing pages):
  - Volume-name input with inline validation (allowed charset, non-empty) and a hint.
  - Drag-and-drop zone + a file-picker button (accept image types). Dropped/picked
    files accumulate in a removable list showing name + human size; client-side filter
    to image types with a notice for skipped files.
  - **Ingest** button (disabled until a valid name + ≥1 file): calls
    `uploadVolume`, shows overall progress and a busy state, then per-result summary.
  - On success: confirmation with the returned `page_count`, plus links to `/batches`
    and `/viewer/<id>`. On error: surfaces the backend message.
- The `/batches` list page (`routes/batches/+page.svelte`) gets a **"New volume"**
  button linking to `/batches/new`.

### 4. Infra / wiring

- No new services, ports, or env. MinIO stays internal.
- **Known limitation (noted, not fixed):** the gateway proxies by reading the full
  body (`await request.body()`), so uploads are buffered in memory at the gateway
  (and spooled by FastAPI `UploadFile` at core-api). Fine for typical drop-a-batch
  sizes; a streaming proxy is a future optimization, out of scope. Document a
  sensible practical ceiling in the UI copy if needed.

### 5. Testing / verification

- **Backend unit (moto + sqlite)**, mirroring `test_register_endpoint.py`:
  - upload N image files → 201, objects exist at `images-batch/<id>/`,
    `page_count == N`, `manifest_status == ok`.
  - mixed selection → only images written, non-images reported/skipped.
  - all-non-image → 422; bad volume id (slash/space/`..`) → 422; path-traversal
    filename (`../evil.jpg`) → written as basename only (no escape).
  - re-upload is idempotent (page_count refreshes, chunk_id preserved).
- **Lint/type:** `ruff` + `ty` green on touched Python; frontend `bun run check`
  green on touched Svelte/TS.
- **Integration (manual, running stack):** open `/batches/new`, drop the htrdemo
  images, confirm 201, the volume appears in `/batches`, and pages render in
  `/viewer/<id>`.

## Out of scope / follow-ups

- Triggering HTR from the UI (needs the orchestrator compose-submission gap closed).
- Streaming upload proxy through the gateway (current buffering is acceptable).
- Presigned/direct-to-MinIO uploads (rejected — would expose MinIO).
- Folder/zip upload, resumable/chunked uploads, dedup — YAGNI for now.

## Verification (2026-06-22)

Implemented via subagent-driven-development on branch `feat/batches-upload`. End-to-end verified on the running compose stack:
- `POST /api/batches/uitest/upload` with 2 images → **201**, `page_count=2`, objects written to `images-batch/uitest/`, volume appears in `/api/batches/`.
- Bad volume id (`bad.id`) → **422** `application/problem+json` (`UnprocessableEntityError`).
- `/batches/new` renders (drop zone, volume-name field); `/batches` shows the "New volume" button.
- Backend: 5/5 unit tests (moto) pass; ruff + ty clean. Frontend: `bun run check` 0/0.

Build note: fleet images have no `build:` in docker-compose.yml — build them with `docker buildx -f .docker/<svc>.dockerfile` (the Makefile way), not `docker compose build`. uv reuses a cached workspace wheel when the package version is unchanged, so a `--no-cache` (or version bump) is needed to pick up source-only changes to `core` in the image.
