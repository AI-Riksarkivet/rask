# Batches Upload/Ingest View — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user drop/select images under a volume name in the browser and have them uploaded into MinIO and registered as a `batches` row — appearing in **Batches** ready for HTR.

**Architecture:** Browser (drop zone at `/batches/new`) → `FormData` POST → ingress → gateway (`/api/*`) → new core-api endpoint `POST /api/batches/{id}/upload` → writes each file to `images-batch/<id>/` via core-api's internal S3 client → `register_volume` → returns `BatchPublic`. MinIO stays internal (no presigned, no published `:9000`, no CORS).

**Tech Stack:** Python 3.13 + FastAPI + boto3 (core-api), SQLModel, `moto`/pytest; TypeScript `@rask/api`; SvelteKit 2 + Svelte 5 (runes) + `@rask/ui` (shadcn-svelte) frontend; uv + Bun + Turborepo.

## Global Constraints

- Commits: **no Claude/AI co-author trailer.**
- Python: **Ruff line length 160**; `ty` has **error-on-warning = true** (warnings fail). Run `uv run ruff check <files>` and `uvx ty check <dir>`.
- Frontend: **Svelte 5 strict** — browser-only globals stay inside handlers/`$effect`/`onMount`; use Svelte 5 event attrs (`onclick`, `ondrop`), not `on:click`. Gate with `bun --cwd components/apps/frontend run check`.
- **Volume id charset:** `^[A-Za-z0-9_-]+$` (letters, digits, `-`, `_` only — no dots/slashes/spaces). Invalid → `422`.
- **Image suffixes:** `.jpg .jpeg .png .tif .tiff` (case-insensitive). Non-images skipped; all-non-image upload → `422`.
- **Filename safety:** store basename only (`PurePosixPath(name).name`) — no path traversal.
- **API prefix:** tests run with the default prefix `/api/v1` (so test URLs are `/api/v1/batches/...`); the compose runtime uses `/api` via `RASK_API_PREFIX` — do not hardcode `/api/v1` in `@rask/api` (client uses relative `/api/...` which the ingress/gateway route).
- The upload endpoint returns `BatchPublic` (snake_case) → matches `@rask/api`'s `BatchRow`.
- Reuse `register_volume` and `S3Dep` — no new S3 wiring.

---

### Task 1: Backend — `POST /api/batches/{id}/upload` on core-api

**Files:**
- Modify: `components/services/core/src/core/api/v1/endpoints/batches.py`
- Test: `components/services/core/tests/test_upload_endpoint.py` (create)

**Interfaces:**
- Consumes: `registration.register_volume(session, s3, *, input_bucket, volume_id) -> Batch`; `S3Dep` (boto3 client with `.put_object`); `settings.cache_bucket`; `service_kit.exceptions.ValidationError` (→ 422).
- Produces: `POST {prefix}/batches/{batch_id}/upload` accepting multipart field `files` (repeatable), returning `BatchPublic` (201).

- [ ] **Step 1: Write the failing test**

Create `components/services/core/tests/test_upload_endpoint.py`:

```python
"""POST /batches/{id}/upload — multipart upload writes to the input bucket + registers.

Mirrors test_register_endpoint.py: schema via a sync engine, lifespan S3 builder
patched to a moto client. The endpoint itself does the writes (nothing pre-seeded).
"""

from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws
from sqlalchemy import create_engine
from sqlmodel import SQLModel

from core.models.batch import Batch  # noqa: F401 - registers table with SQLModel.metadata


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[TestClient, object]]:
    db = tmp_path / "b.db"
    engine = create_engine(f"sqlite:///{db}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()

    monkeypatch.setenv("RASK_VIEWER_INPUT", "s3://images-batch")
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", "s3://images-batch-alto")
    monkeypatch.setenv("RASK_CACHE_BUCKET", "images-batch")
    monkeypatch.setenv("RASK_BATCHES_DB", str(db))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("RAY_DASHBOARD_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("HCP_ENDPOINT", "http://localhost:9000")

    with mock_aws():
        c = boto3.client("s3", region_name="us-east-1")
        c.create_bucket(Bucket="images-batch")
        monkeypatch.setattr("core.lifespan._build_s3", lambda _settings: c)

        from core.main import create_app

        with TestClient(create_app()) as tc:
            yield tc, c


def _keys(s3: object, prefix: str = "") -> list[str]:
    resp = s3.list_objects_v2(Bucket="images-batch", Prefix=prefix)  # type: ignore[attr-defined]
    return sorted(o["Key"] for o in resp.get("Contents", []))


def test_upload_creates_and_registers(client: tuple[TestClient, object]) -> None:
    tc, s3 = client
    files = [
        ("files", ("page_0001.jpg", b"img1", "image/jpeg")),
        ("files", ("page_0002.jpg", b"img2", "image/jpeg")),
        ("files", ("page_0003.png", b"img3", "image/png")),
    ]
    resp = tc.post("/api/v1/batches/myvol/upload", files=files)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["batch_id"] == "myvol"
    assert body["page_count"] == 3
    assert body["manifest_status"] == "ok"
    assert _keys(s3, "myvol/") == ["myvol/page_0001.jpg", "myvol/page_0002.jpg", "myvol/page_0003.png"]


def test_upload_skips_non_images(client: tuple[TestClient, object]) -> None:
    tc, s3 = client
    files = [
        ("files", ("a.jpg", b"img", "image/jpeg")),
        ("files", ("notes.txt", b"hi", "text/plain")),
    ]
    resp = tc.post("/api/v1/batches/mixed/upload", files=files)
    assert resp.status_code == 201, resp.text
    assert resp.json()["page_count"] == 1
    assert _keys(s3, "mixed/") == ["mixed/a.jpg"]  # txt not written


def test_upload_all_non_images_422(client: tuple[TestClient, object]) -> None:
    tc, _ = client
    resp = tc.post("/api/v1/batches/novol/upload", files=[("files", ("notes.txt", b"hi", "text/plain"))])
    assert resp.status_code == 422


def test_upload_bad_volume_id_422(client: tuple[TestClient, object]) -> None:
    tc, _ = client
    # dot is disallowed by the volume-id charset
    resp = tc.post("/api/v1/batches/bad.id/upload", files=[("files", ("a.jpg", b"x", "image/jpeg"))])
    assert resp.status_code == 422


def test_upload_filename_is_basenamed(client: tuple[TestClient, object]) -> None:
    tc, s3 = client
    resp = tc.post("/api/v1/batches/safe/upload", files=[("files", ("../evil.jpg", b"x", "image/jpeg"))])
    assert resp.status_code == 201, resp.text
    # stored as basename under the volume prefix; no traversal out of safe/
    assert _keys(s3) == ["safe/evil.jpg"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest components/services/core/tests/test_upload_endpoint.py -v`
Expected: FAIL — `404 Not Found` (endpoint doesn't exist yet).

- [ ] **Step 3: Implement the endpoint**

In `components/services/core/src/core/api/v1/endpoints/batches.py`:

Update the imports at the top — change the `fastapi` import and the `service_kit.exceptions` import, and add stdlib imports:

```python
import mimetypes
import re
from datetime import timedelta
from pathlib import PurePosixPath

from anyio import to_thread
from fastapi import APIRouter, File, UploadFile, status

from core.api.dependencies import CatalogTblDep, S3Dep, SessionDep, SettingsDep
from core.models.batch import BatchPublic
from core.models.enums import HtrStatus
from core.schemas.batch import BatchListResponse, RandomBatchResponse
from core.schemas.catalog import CatalogHit
from core.schemas.sync import SyncResponse
from core.services import batches as batches_service
from core.services import registration
from core.services.discover import catalog as catalog_service
from core.services.sync import reconcile_from_s3
from service_kit.exceptions import NotFoundError, ValidationError
```

Add module-level constants (after the imports, before `router`):

```python
_VOLUME_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".tif", ".tiff")
```

Add the endpoint (place it right after the existing `register_batch` endpoint):

```python
@router.post("/{batch_id}/upload", status_code=status.HTTP_201_CREATED)
async def upload_batch(
    batch_id: str,
    session: SessionDep,
    settings: SettingsDep,
    s3: S3Dep,
    files: list[UploadFile] = File(...),
) -> BatchPublic:
    """Upload image files to `images-batch/<batch_id>/`, then register the volume.

    Browser -> gateway -> here -> internal S3 put -> register_volume. Non-image
    files are skipped; filenames are reduced to their basename (no path traversal).
    """
    if not _VOLUME_ID_RE.match(batch_id):
        raise ValidationError(f"invalid volume id {batch_id!r}: use letters, digits, '-', '_' only")

    written = 0
    for upload in files:
        name = PurePosixPath(upload.filename or "").name  # basename only — blocks ../ traversal
        if not name or not name.lower().endswith(_IMAGE_SUFFIXES):
            continue
        data = await upload.read()
        key = f"{batch_id}/{name}"
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        await to_thread.run_sync(
            lambda k=key, d=data, ct=content_type: s3.put_object(
                Bucket=settings.cache_bucket, Key=k, Body=d, ContentType=ct
            )
        )
        written += 1

    if written == 0:
        raise ValidationError("no image files in upload (allowed: jpg, jpeg, png, tif, tiff)")

    batch = await registration.register_volume(session, s3, input_bucket=settings.cache_bucket, volume_id=batch_id)
    return BatchPublic.model_validate(batch)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest components/services/core/tests/test_upload_endpoint.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Lint + typecheck**

Run: `uv run ruff check components/services/core/src/core/api/v1/endpoints/batches.py components/services/core/tests/test_upload_endpoint.py`
Run: `uvx ty check components/services/core/src`
Expected: both clean (no new errors).

- [ ] **Step 6: Commit**

```bash
git add components/services/core/src/core/api/v1/endpoints/batches.py components/services/core/tests/test_upload_endpoint.py
git commit -m "feat(core-api): POST /batches/{id}/upload — multipart upload + register"
```

---

### Task 2: API client — `uploadVolume` + `registerVolume` in `@rask/api`

**Files:**
- Modify: `packages/api/src/batches.ts`

**Interfaces:**
- Consumes: backend `POST /api/batches/{id}/upload` (multipart `files`) and `POST /api/batches/{id}/register`, both returning a `BatchRow`.
- Produces: `uploadVolume(id: string, files: File[]): Promise<BatchRow>` and `registerVolume(id: string): Promise<BatchRow>` (re-exported via `packages/api/src/index.ts`'s existing `export * from './batches'`).

- [ ] **Step 1: Add the client functions**

Append to `packages/api/src/batches.ts` (after `syncBatches`):

```typescript
// ---------- Ingest (upload + register) ----------

/** Upload image files into images-batch/<id>/ and register the volume. Returns the new batch row. */
export async function uploadVolume(id: string, files: File[]): Promise<BatchRow> {
	const form = new FormData();
	for (const f of files) form.append('files', f, f.name);
	const res = await fetch(`/api/batches/${encodeURIComponent(id)}/upload`, { method: 'POST', body: form });
	if (!res.ok) {
		const body = await res.text();
		throw new Error(`uploadVolume(${id}): HTTP ${res.status}: ${body.slice(0, 300)}`);
	}
	return res.json();
}

/** Register an already-uploaded volume prefix (images must already be in the bucket). */
export async function registerVolume(id: string): Promise<BatchRow> {
	const res = await fetch(`/api/batches/${encodeURIComponent(id)}/register`, { method: 'POST' });
	if (!res.ok) {
		const body = await res.text();
		throw new Error(`registerVolume(${id}): HTTP ${res.status}: ${body.slice(0, 300)}`);
	}
	return res.json();
}
```

- [ ] **Step 2: Typecheck the package via the frontend**

Run: `bun --cwd components/apps/frontend run check`
Expected: no new type errors (functions resolve, `BatchRow` import valid).

- [ ] **Step 3: Commit**

```bash
git add packages/api/src/batches.ts
git commit -m "feat(api): uploadVolume + registerVolume client functions"
```

---

### Task 3: Frontend — `/batches/new` route + "New volume" button

**Files:**
- Create: `components/apps/frontend/src/routes/batches/new/+page.svelte`
- Modify: `components/apps/frontend/src/routes/batches/+page.svelte` (add a link button)

**Interfaces:**
- Consumes: `uploadVolume(id, files)` and `type BatchRow` from `@rask/api`; `RayShell`, `Card`, `Button` (same imports the existing batches page uses); `goto` from `$app/navigation`.
- Produces: a user-facing route at `/batches/new`.

- [ ] **Step 1: Create the route component**

Create `components/apps/frontend/src/routes/batches/new/+page.svelte`:

```svelte
<script lang="ts">
	import { uploadVolume, type BatchRow } from '@rask/api';
	import RayShell from '$lib/components/layout/ray-shell.svelte';
	import { Card } from '$lib/components/ui/card';
	import { Button } from '@rask/ui/button';
	import { Upload, X, FileImage } from 'lucide-svelte';

	const IMAGE_RE = /\.(jpe?g|png|tiff?)$/i;
	const ID_RE = /^[A-Za-z0-9_-]+$/;

	let volumeId = $state('');
	let files = $state<File[]>([]);
	let dragOver = $state(false);
	let busy = $state(false);
	let error = $state<string | null>(null);
	let result = $state<BatchRow | null>(null);

	const validId = $derived(ID_RE.test(volumeId));
	const canIngest = $derived(validId && files.length > 0 && !busy);

	function addFiles(list: FileList | null) {
		if (!list) return;
		const names = new Set(files.map((f) => f.name));
		const incoming = Array.from(list).filter((f) => IMAGE_RE.test(f.name) && !names.has(f.name));
		files = [...files, ...incoming];
	}
	function removeFile(name: string) {
		files = files.filter((f) => f.name !== name);
	}
	function onDrop(e: DragEvent) {
		e.preventDefault();
		dragOver = false;
		addFiles(e.dataTransfer?.files ?? null);
	}
	function fmtSize(n: number): string {
		if (n < 1024) return `${n} B`;
		if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
		return `${(n / 1024 / 1024).toFixed(1)} MB`;
	}
	async function ingest() {
		busy = true;
		error = null;
		result = null;
		try {
			result = await uploadVolume(volumeId, files);
		} catch (e: unknown) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			busy = false;
		}
	}
</script>

<RayShell title="New volume">
	<Card class="m-4 max-w-2xl space-y-4 p-6">
		<h1 class="text-lg font-semibold">Upload images</h1>

		<label class="block space-y-1">
			<span class="text-sm font-medium">Volume name</span>
			<input
				class="bg-background w-full rounded border px-3 py-2"
				placeholder="e.g. my_volume"
				bind:value={volumeId}
				disabled={busy}
			/>
			{#if volumeId && !validId}
				<span class="text-destructive text-xs">Letters, digits, - and _ only.</span>
			{/if}
		</label>

		<div
			role="button"
			tabindex="0"
			class="rounded border-2 border-dashed p-8 text-center {dragOver ? 'border-primary bg-muted' : 'border-muted'}"
			ondragover={(e) => {
				e.preventDefault();
				dragOver = true;
			}}
			ondragleave={() => (dragOver = false)}
			ondrop={onDrop}
		>
			<Upload class="mx-auto mb-2 h-6 w-6 opacity-60" />
			<p class="text-sm">Drag &amp; drop images here, or</p>
			<label class="mt-2 inline-block">
				<input
					type="file"
					multiple
					accept="image/*"
					class="hidden"
					onchange={(e) => addFiles((e.currentTarget as HTMLInputElement).files)}
				/>
				<span class="cursor-pointer text-sm underline">browse</span>
			</label>
			<p class="mt-1 text-xs opacity-60">jpg, png, tif</p>
		</div>

		{#if files.length}
			<ul class="space-y-1 text-sm">
				{#each files as f (f.name)}
					<li class="flex items-center justify-between rounded border px-2 py-1">
						<span class="flex items-center gap-2 truncate"><FileImage class="h-4 w-4 opacity-60" />{f.name}</span>
						<span class="flex items-center gap-2 opacity-60">
							{fmtSize(f.size)}
							<button onclick={() => removeFile(f.name)} disabled={busy} aria-label="remove file">
								<X class="h-4 w-4" />
							</button>
						</span>
					</li>
				{/each}
			</ul>
		{/if}

		<Button onclick={ingest} disabled={!canIngest}>
			{busy ? 'Ingesting…' : `Ingest ${files.length || ''} image${files.length === 1 ? '' : 's'}`}
		</Button>

		{#if error}<p class="text-destructive text-sm">{error}</p>{/if}
		{#if result}
			<div class="space-y-1 rounded border border-green-600 p-3 text-sm">
				<p>Ingested <strong>{result.batch_id}</strong> — {result.page_count} pages.</p>
				<div class="flex gap-3">
					<a class="underline" href="/batches">Back to Batches</a>
					<a class="underline" href={`/viewer/${result.batch_id}`}>Open viewer</a>
				</div>
			</div>
		{/if}
	</Card>
</RayShell>
```

- [ ] **Step 2: Add a "New volume" button on the batches list page**

In `components/apps/frontend/src/routes/batches/+page.svelte`:

Add `goto` to the existing `$app/navigation` usage — at the top of the `<script>` add:

```typescript
import { goto } from '$app/navigation';
```

Find the toolbar where the existing Sync/Refresh `<Button>`s render (search for `syncBatches` / `RefreshCw` in the markup) and add, next to them:

```svelte
<Button onclick={() => goto('/batches/new')}>New volume</Button>
```

- [ ] **Step 3: Typecheck + Svelte check**

Run: `bun --cwd components/apps/frontend run check`
Expected: no errors. If a component import path or a Svelte 5 event binding is flagged, align it with the existing `routes/batches/+page.svelte` (the reference for `Card`/`Button`/`RayShell` imports and event syntax) and re-run.

- [ ] **Step 4: Commit**

```bash
git add components/apps/frontend/src/routes/batches/new/+page.svelte components/apps/frontend/src/routes/batches/+page.svelte
git commit -m "feat(frontend): /batches/new drop-images upload view + Batches link"
```

---

### Task 4: End-to-end verification on the running compose stack

**Files:** none (verification only).

- [ ] **Step 1: Rebuild the two changed images + recreate**

The new endpoint is in the core-api image; the route is in the frontend image. Rebuild both and recreate (docker needs `sg docker -c`):

```bash
sg docker -c 'docker compose -f /home/morgan/rask-main/docker-compose.yml build core-api frontend'
sg docker -c 'docker compose -f /home/morgan/rask-main/docker-compose.yml up -d core-api frontend'
```
Expected: both rebuild; services return to healthy.

- [ ] **Step 2: API smoke (through the ingress)**

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8888/api/batches/uitest/upload \
  -F "files=@/tmp/htrdemo/A0062408_00006.jpg" -F "files=@/tmp/htrdemo/451511_1512_01.jpg"
```
Expected: `201`. Then `curl -s http://localhost:8888/api/batches/ | grep -o uitest` → prints `uitest`.

- [ ] **Step 3: Browser check**

Open http://localhost:8888/batches/new — enter a volume name, drop a couple of `/tmp/htrdemo/*.jpg`, click **Ingest**. Expect the success panel (page count), and the volume visible at http://localhost:8888/batches with pages rendering at `/viewer/<name>`.

- [ ] **Step 4: Record + commit verification note**

Append a short "verified" line to the spec's testing section and commit:

```bash
git add docs/superpowers/specs/2026-06-22-batches-upload-view-design.md
git commit -m "docs: record batches-upload end-to-end verification"
```

---

## Notes for the implementer

- **HTR is not triggered** by this feature — ingest only makes the volume appear in Batches (per the spec scope). Transcription remains the runner step.
- **Gateway buffers the upload body** (`await request.body()` in the gateway proxy) — fine for normal batches; do not try to "fix" it here.
- Do not expose MinIO or add presigned URLs — the whole point is uploads go through the backend.
