# File Handling

Serving and accepting files in FastAPI: downloads (`FileResponse`), generated content (`StreamingResponse`), range requests, uploads (`UploadFile`), and temp-file cleanup.

> For pure data streams (JSON Lines, Server-Sent Events, byte streams without a file), see [`streaming.md`](streaming.md). This reference is about **files** — content with a filename, MIME type, and a Content-Disposition.

## Contents

- Pick the right response type
- Downloads from disk — `FileResponse`
- Generated content — `StreamingResponse` (CSV, ZIP, async file streaming)
- Range requests — video / resumable downloads
- Uploads — `UploadFile` with chunked reads
- Temp files & guaranteed cleanup (generator-with-`finally` vs `BackgroundTasks`)
- Filename sanitization (including RFC 5987 for non-ASCII)
- Anti-patterns

## Pick the right response type

| Scenario                                              | Use                                | Memory   |
| ----------------------------------------------------- | ---------------------------------- | -------- |
| File already exists on disk, any size                 | `FileResponse`                     | constant |
| Generated content (CSV, ZIP, PDF), any size           | `StreamingResponse(generator)`     | constant |
| Tiny file (< 1 MB), already in memory                 | `Response(content=bytes)`          | bounded  |
| Video / large download, client may resume             | `StreamingResponse` + range header | constant |
| Anything else                                         | start with `FileResponse`          | —        |

Default to `FileResponse` if the bytes exist on disk — it streams under the hood, sets Content-Type from the extension, and handles `If-Modified-Since`. Reach for `StreamingResponse` only when you're generating bytes or doing range-aware partial reads.

## Downloads from disk — `FileResponse`

```python
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter()
DOWNLOADS = Path("/srv/downloads").resolve()


@router.get("/files/{name}")
async def download_file(name: str) -> FileResponse:
    target = (DOWNLOADS / name).resolve()

    # Path-traversal guard — required when `name` comes from user input.
    if not target.is_relative_to(DOWNLOADS):
        raise HTTPException(400, "invalid path")
    if not target.is_file():
        raise HTTPException(404, "not found")

    return FileResponse(
        path=target,
        filename=target.name,                  # sets Content-Disposition
        media_type="application/octet-stream", # force download (vs inline render)
    )
```

The `filename=` arg is what tells the browser to save instead of display. Omit it (or set `media_type` to e.g. `application/pdf`) to render inline.

### `inline` vs `attachment`

```python
# Inline (browser tries to display) — PDFs, images, plaintext.
return FileResponse(target, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{target.name}"'})

# Attachment (force save dialog).
return FileResponse(target, filename=target.name)  # FileResponse defaults to attachment
```

### Caching headers

For immutable / hashed-name assets:

```python
return FileResponse(
    target,
    filename=target.name,
    headers={"Cache-Control": "private, max-age=3600, immutable"},
)
```

## Generated content — `StreamingResponse`

When you don't have bytes on disk, build them lazily so memory stays flat.

### CSV export, row-by-row

```python
import csv
import io
from collections.abc import AsyncIterator

from fastapi.responses import StreamingResponse


async def stream_csv(session: SessionDep) -> AsyncIterator[str]:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "email", "created_at"])
    yield buf.getvalue()
    buf.seek(0); buf.truncate()

    async for batch in session.stream(select(User).execution_options(yield_per=1000)):
        for u in batch:
            writer.writerow([u.id, u.email, u.created_at])
        yield buf.getvalue()
        buf.seek(0); buf.truncate()


@router.get("/exports/users.csv")
async def export_users(session: SessionDep) -> StreamingResponse:
    return StreamingResponse(
        stream_csv(session),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="users.csv"'},
    )
```

### ZIP archive, on the fly

```python
import io, zipfile
from collections.abc import Iterator
from pathlib import Path


def stream_zip(files: list[Path]) -> Iterator[bytes]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            if not f.is_file():
                continue
            zf.write(f, arcname=f.name)
            buf.seek(0); chunk = buf.read(); buf.seek(0); buf.truncate()
            if chunk:
                yield chunk
    # Footer (central directory) lands here after `with` exits.
    buf.seek(0)
    if (footer := buf.read()):
        yield footer
```

### Async file reads — when?

Use `aiofiles` for **disk reads inside an `async def` route**:

```python
import aiofiles
from collections.abc import AsyncIterator


async def stream_disk(path: Path, chunk: int = 1 << 20) -> AsyncIterator[bytes]:
    async with aiofiles.open(path, "rb") as f:
        while data := await f.read(chunk):
            yield data
```

For pure file-from-disk responses, **prefer `FileResponse`** — Starlette already uses an efficient async-friendly send path (sendfile where available). `aiofiles` is only needed when you're interleaving disk reads with async work (DB lookups, HTTP calls) in the same generator.

## Range requests — video / resumable downloads

```python
import re
from fastapi import Request
from fastapi.responses import StreamingResponse


def _parse_range(header: str, size: int) -> tuple[int, int]:
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not m:
        return 0, size - 1
    a, b = m.groups()
    if a and b:
        return int(a), min(int(b), size - 1)
    if a:
        return int(a), size - 1
    if b:
        return max(0, size - int(b)), size - 1
    return 0, size - 1


async def _range_iter(path: Path, start: int, end: int, chunk: int = 1 << 16):
    async with aiofiles.open(path, "rb") as f:
        await f.seek(start)
        remaining = end - start + 1
        while remaining > 0 and (data := await f.read(min(chunk, remaining))):
            remaining -= len(data)
            yield data


@router.get("/videos/{name}")
async def stream_video(name: str, request: Request) -> StreamingResponse:
    target = (VIDEOS / name).resolve()
    if not target.is_relative_to(VIDEOS) or not target.is_file():
        raise HTTPException(404)
    size = target.stat().st_size

    if (rng := request.headers.get("range")):
        start, end = _parse_range(rng, size)
        return StreamingResponse(
            _range_iter(target, start, end),
            status_code=206,
            media_type="video/mp4",
            headers={
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(end - start + 1),
                "Accept-Ranges": "bytes",
            },
        )

    return StreamingResponse(
        _range_iter(target, 0, size - 1),
        media_type="video/mp4",
        headers={"Content-Length": str(size), "Accept-Ranges": "bytes"},
    )
```

If you only need plain video streaming without HTTP-range support, plain `FileResponse(media_type="video/mp4")` works — most browsers will fall back to non-ranged playback. The range path matters when users seek.

## Uploads — `UploadFile`

```python
from typing import Annotated
from fastapi import APIRouter, File, UploadFile, HTTPException

router = APIRouter()
MAX_MB = 50


@router.post("/upload")
async def upload(file: Annotated[UploadFile, File(...)]) -> dict[str, object]:
    # Validate before reading — `UploadFile.size` is set when the client sends Content-Length.
    if file.size is not None and file.size > MAX_MB * 1024 * 1024:
        raise HTTPException(413, f"file too large (>{MAX_MB} MB)")

    target = (UPLOADS / sanitize_filename(file.filename or "upload.bin")).resolve()
    if not target.is_relative_to(UPLOADS):
        raise HTTPException(400, "invalid filename")

    async with aiofiles.open(target, "wb") as out:
        while chunk := await file.read(1 << 20):   # 1 MiB chunks; backpressure-friendly
            await out.write(chunk)

    return {"saved": target.name, "size": target.stat().st_size}
```

- **Never** call `await file.read()` without a size — that loads the whole upload into memory.
- For multi-file uploads, use `list[UploadFile]`. For form fields alongside, use `Form(...)`.
- For uploads that flow straight to S3 / GCS, use the storage client's streaming upload API and pass `file.file` (a SpooledTemporaryFile-like object) — no on-disk staging needed.

## Temp files & guaranteed cleanup

Generated downloads often need a temp file. Two patterns; pick by your risk tolerance.

### A. Generator-with-`finally` — guaranteed cleanup

The file is unlinked when the stream finishes (success or client disconnect):

```python
import tempfile
from collections.abc import AsyncIterator


async def _stream_then_delete(path: Path) -> AsyncIterator[bytes]:
    try:
        async with aiofiles.open(path, "rb") as f:
            while data := await f.read(1 << 20):
                yield data
    finally:
        path.unlink(missing_ok=True)


@router.get("/reports/{rid}")
async def report(rid: str) -> StreamingResponse:
    tmp = Path(tempfile.mkstemp(suffix=".pdf", dir="/tmp")[1])
    await render_report_to(tmp, rid)
    return StreamingResponse(
        _stream_then_delete(tmp),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="report-{rid}.pdf"'},
    )
```

### B. `BackgroundTasks` cleanup — best-effort

OK *only* when cleanup failure has no user impact (orphaned temp files are reaped by a cron). Loses the file if the worker dies mid-response.

```python
from fastapi import BackgroundTasks


@router.get("/reports/{rid}")
async def report(rid: str, tasks: BackgroundTasks) -> FileResponse:
    tmp = Path(tempfile.mkstemp(suffix=".pdf", dir="/tmp")[1])
    await render_report_to(tmp, rid)
    tasks.add_task(tmp.unlink, missing_ok=True)
    return FileResponse(tmp, media_type="application/pdf", filename=f"report-{rid}.pdf")
```

> See `anti-patterns.md` — `BackgroundTasks` for anything you'd page on is a footgun. Temp file cleanup is the rare case where best-effort is actually fine.

## Filename sanitization

Anything in `filename=` that originated from user input must be sanitized — both for the path on disk and for the `Content-Disposition` header.

```python
import re


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[/\\\x00:<>\"|?*]", "_", name).strip(". ")
    return name or "download.bin"
```

For non-ASCII filenames, send both the plain `filename=` (latin-1 only) and `filename*=` (RFC 5987):

```python
from urllib.parse import quote


cd = f'attachment; filename="{sanitize_filename(display)}"; filename*=UTF-8\'\'{quote(display)}'
return FileResponse(target, headers={"Content-Disposition": cd})
```

## Anti-patterns

| Mistake                                                          | Why                                                                                                  |
| ---------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `return open(path, "rb").read()` from a route                    | Loads the whole file into memory. Use `FileResponse`.                                                |
| `await file.read()` without a size cap on uploads                | Bounded only by client patience. Read in 1 MiB chunks; check `file.size` first.                      |
| Using `aiofiles` to serve a static file when `FileResponse` would | Reinventing what Starlette already does (sendfile-aware).                                            |
| `media_type="application/octet-stream"` for previewable content  | Browser will always download. Set the real MIME type if `inline` is wanted.                          |
| User-controlled path joined without `resolve()` + `is_relative_to` check | Directory traversal — `../../etc/passwd` reads off-tree.                                       |
| Storing temp files in the app's working directory                | Container restarts wipe them mid-stream; permissions break. Use `tempfile.gettempdir()` or a dedicated volume. |
| Forgetting `Accept-Ranges: bytes` on a video route               | Some players probe with `HEAD` first; without the header they download the whole file then seek.     |
| `TempFileManager` singleton class for one cleanup case           | Over-engineered. A 6-line generator with `try / finally` is sufficient.                              |
| `@app.on_event("startup")` to clean orphaned temps               | Deprecated event; use lifespan. Or run as a cron — startup shouldn't do filesystem janitor work.     |
