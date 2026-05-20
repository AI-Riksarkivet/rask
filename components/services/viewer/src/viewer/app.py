"""FastAPI app — JSON endpoints + (optional) static SPA serving.

Endpoints:
    GET  /api/health
    GET  /api/volumes/{vol}/pages              -> [{key, hasAlto}]
    GET  /api/volumes/{vol}/pages/{key}/image  -> raw image bytes
    GET  /api/volumes/{vol}/pages/{key}/alto   -> ALTO XML (404 if absent)
    GET  /api/batches                          -> {summary, batches[]} from .cache/batches.db
    GET  /api/batches/{batch_id}               -> single batch row
    POST /api/batches/sync                     -> kicks scripts/sync_from_s3.py
    GET  /                                     -> built SvelteKit SPA (if frontend/build exists)
"""

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from storage import build_source, derive_hcp_creds
from viewer import ray_dashboard


# Repo root: .../components/services/viewer/src/viewer/app.py → up 5
_REPO = Path(__file__).resolve().parents[5]
_BATCHES_DB = _REPO / ".cache" / "batches.db"
_SYNC_SCRIPT = _REPO / "scripts" / "sync_from_s3.py"


load_dotenv()
derive_hcp_creds()


def _input_uri() -> str:
    return os.environ["RASK_VIEWER_INPUT"]


def _output_uri() -> str:
    return os.environ["RASK_VIEWER_OUTPUT"]


def _endpoint() -> str | None:
    return os.getenv("HCP_ENDPOINT")


def create_app() -> FastAPI:
    # Disable FastAPI's default Swagger/ReDoc HTML — those load JS+CSS from
    # cdn.jsdelivr.net, which port-forward layers (VS Code Remote, Codespaces,
    # corp proxies) often block via CSP, leaving the docs page blank. We
    # serve our own bundles same-origin under /api/_static/ instead.
    app = FastAPI(
        title="viewer",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    # Vendored swagger-ui + redoc bundles (components/services/viewer/src/viewer/static/).
    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/api/_static", StaticFiles(directory=str(static_dir)), name="api-static")

    @app.get("/api/docs", include_in_schema=False)
    def swagger_ui_html() -> Response:
        from fastapi.openapi.docs import get_swagger_ui_html

        return get_swagger_ui_html(
            openapi_url=app.openapi_url or "/api/openapi.json",
            title=f"{app.title} — Swagger UI",
            swagger_js_url="/api/_static/swagger-ui-bundle.js",
            swagger_css_url="/api/_static/swagger-ui.css",
        )

    @app.get("/api/redoc", include_in_schema=False)
    def redoc_html() -> Response:
        from fastapi.openapi.docs import get_redoc_html

        return get_redoc_html(
            openapi_url=app.openapi_url or "/api/openapi.json",
            title=f"{app.title} — ReDoc",
            redoc_js_url="/api/_static/redoc.standalone.js",
        )

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "input": _input_uri(), "output": _output_uri()}

    @app.get("/api/volumes/{vol}/pages")
    def list_pages(vol: str) -> list[dict]:
        # Source listings are scoped by prefix (volume name).
        prefix = vol.rstrip("/") + "/"
        images_src = build_source(_input_uri(), s3_endpoint=_endpoint(), prefix=prefix)
        altos_src = build_source(_output_uri(), s3_endpoint=_endpoint(), prefix=prefix, suffixes=(".xml",))

        alto_keys = frozenset(altos_src.keys())
        out = []
        for key in sorted(images_src.keys()):
            stem = key.rsplit(".", 1)[0]
            out.append({"key": key, "hasAlto": stem + ".xml" in alto_keys})
        return out

    @app.get("/api/volumes/{vol}/pages/{key:path}/image")
    def get_image(vol: str, key: str) -> Response:
        # `key` arrives URL-decoded. Trust the caller passed a valid relative path under {vol}/.
        src = build_source(_input_uri(), s3_endpoint=_endpoint())
        try:
            data = src.read(key)
        except Exception as e:
            raise HTTPException(404, f"Image not found: {key}") from e
        return Response(content=data, media_type=_image_mime(key))

    @app.get("/api/volumes/{vol}/pages/{key:path}/alto")
    def get_alto(vol: str, key: str) -> Response:
        # `key` is the IMAGE key (e.g. "A0060198/A0060198_00012.jpg"); ALTO is the same path with .xml.
        alto_key = key.rsplit(".", 1)[0] + ".xml"
        src = build_source(_output_uri(), s3_endpoint=_endpoint(), suffixes=(".xml",))
        try:
            data = src.read(alto_key)
        except Exception as e:
            raise HTTPException(404, f"ALTO not found: {alto_key}") from e
        return Response(content=data, media_type="application/xml")

    _register_batches_routes(app)
    _register_ray_routes(app)
    _register_orchestrator_routes(app)
    _register_search_routes(app)
    _register_catalog_routes(app)

    # Optional: serve SvelteKit static build at /. The adapter-static fallback
    # is `index.html` — for unknown paths (client-side routes like /viewer/...)
    # we serve that file so the SPA router can take over.
    # __file__ is .../components/services/viewer/src/viewer/app.py — go up 5 to repo root.
    spa_build = Path(__file__).resolve().parents[5] / "frontend" / "build"
    if spa_build.is_dir():
        app.mount("/_app", StaticFiles(directory=spa_build / "_app"), name="spa-app")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str) -> FileResponse:
            # Try a real file in build/ first; fall back to index.html for SPA routes.
            target = spa_build / full_path if full_path else spa_build / "index.html"
            if target.is_file():
                return FileResponse(target)
            return FileResponse(spa_build / "index.html")

    return app


def _list_batches() -> dict:
    if not _BATCHES_DB.exists():
        raise HTTPException(503, "batches.db not built — run scripts/build_batches_db.py")
    db = sqlite3.connect(_BATCHES_DB)
    db.row_factory = sqlite3.Row
    try:
        batches = [dict(r) for r in db.execute("SELECT * FROM batches ORDER BY batch_id")]
        accessible = dict(
            next(
                db.execute(
                    "SELECT COUNT(*) AS batches, COALESCE(SUM(page_count), 0) AS expected, "
                    "COALESCE(SUM(cached_pages), 0) AS cached, COALESCE(SUM(transcribed_pages), 0) AS transcribed "
                    "FROM batches WHERE manifest_status='ok'"
                )
            )
        )
        by_manifest = {r["manifest_status"]: r["n"] for r in db.execute("SELECT manifest_status, COUNT(*) AS n FROM batches GROUP BY 1")}
        by_htr = {r["htr_status"]: r["n"] for r in db.execute("SELECT htr_status, COUNT(*) AS n FROM batches GROUP BY 1")}
        generated_at = next(db.execute("SELECT MAX(last_synced_at) FROM batches"))[0]
    finally:
        db.close()
    return {
        "generated_at": generated_at,
        "summary": {
            "total_batches": len(batches),
            "accessible": accessible,
            "by_manifest_status": by_manifest,
            "by_htr_status": by_htr,
        },
        "batches": batches,
    }


def _list_chunks() -> dict:
    if not _BATCHES_DB.exists():
        raise HTTPException(503, "batches.db not built — run scripts/build_batches_db.py")
    db = sqlite3.connect(_BATCHES_DB)
    db.row_factory = sqlite3.Row
    try:
        chunks = [
            dict(r)
            for r in db.execute(
                """
                SELECT
                    chunk_id,
                    MAX(chunk_total)             AS chunk_total,
                    COUNT(*)                     AS batches,
                    COALESCE(SUM(page_count), 0) AS expected_pages,
                    COALESCE(SUM(cached_pages), 0)      AS cached_pages,
                    COALESCE(SUM(transcribed_pages), 0) AS transcribed_pages,
                    SUM(CASE WHEN htr_status='done' THEN 1 ELSE 0 END) AS done_batches
                FROM batches
                WHERE chunk_id IS NOT NULL
                GROUP BY chunk_id
                ORDER BY chunk_id
                """
            )
        ]
    finally:
        db.close()
    return {"chunks": chunks}


def _register_batches_routes(app: FastAPI) -> None:
    @app.get("/api/batches")
    def list_batches() -> dict:
        return _list_batches()

    @app.get("/api/batches/random")
    def random_batch(status: str = "done") -> dict:
        """Return a random batch matching ``status`` (default: htr_status='done').

        Used by the left-rail Viewer button so a click drops the user
        into a randomly-picked completed batch instead of a static
        landing page.
        """
        if not _BATCHES_DB.exists():
            raise HTTPException(503, "batches.db not built")
        db = sqlite3.connect(_BATCHES_DB)
        try:
            row = db.execute(
                "SELECT batch_id FROM batches WHERE htr_status = ? ORDER BY RANDOM() LIMIT 1",
                (status,),
            ).fetchone()
        finally:
            db.close()
        if not row:
            raise HTTPException(404, f"no batches with htr_status={status!r}")
        return {"batch_id": row[0], "status": status}

    @app.get("/api/batches/{batch_id}")
    def get_batch(batch_id: str) -> dict:
        if not _BATCHES_DB.exists():
            raise HTTPException(503, "batches.db not built")
        db = sqlite3.connect(_BATCHES_DB)
        db.row_factory = sqlite3.Row
        try:
            row = db.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
        finally:
            db.close()
        if row is None:
            raise HTTPException(404, f"unknown batch_id: {batch_id}")
        return dict(row)

    @app.post("/api/batches/sync")
    def sync_batches() -> dict:
        """Run scripts/sync_from_s3.py synchronously and return the new summary."""
        if not _SYNC_SCRIPT.exists():
            raise HTTPException(503, f"missing {_SYNC_SCRIPT}")
        # Same interpreter as the app server — no `uv` needed in deployment.
        proc = subprocess.run(  # noqa: S603 — fixed args, no shell, all paths from repo
            [sys.executable, str(_SYNC_SCRIPT)],
            cwd=_REPO,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if proc.returncode != 0:
            raise HTTPException(500, f"sync failed: {proc.stderr[-500:]}")
        return _list_batches()

    @app.get("/api/chunks")
    def list_chunks() -> dict:
        return _list_chunks()

    @app.post("/api/chunks/{chunk_id}/submit")
    def submit_chunk(chunk_id: int) -> dict:
        """Submit one chunk as a RayJob via scripts/submit_chunks.py --mode local.

        Requires a Ray dashboard at \\$RAY_DASHBOARD_URL (default localhost:8265 —
        spin one up with `make ray-up`). On success returns the chunk's
        submission_id; on Ray-side failure surfaces stderr from the script.
        """
        script = _REPO / "scripts" / "submit_chunks.py"
        if not script.exists():
            raise HTTPException(503, f"missing {script}")
        proc = subprocess.run(  # noqa: S603 — fixed args, no shell, all paths from repo
            [sys.executable, str(script), "--mode", "local", "--chunk", str(chunk_id)],
            cwd=_REPO,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise HTTPException(500, f"submit failed: {proc.stderr[-500:] or proc.stdout[-500:]}")
        return {"chunk_id": chunk_id, "stdout": proc.stdout.strip()}


# Mirror constants from scripts/orchestrator.py so the UI shows the same
# decisions the cron-driven orchestrator would make. If those constants ever
# need to drift apart, factor them into a shared module.
_HTR_READY_FRACTION = 0.95
_FAIL_COOLDOWN_SECS = 600
_CHUNK_RE = re.compile(r"chunk-(\d+)-of-")

# Pipeline stage display order. Names match the Ray Data func_or_class_name
# pattern emitted by /api/v0/tasks/summarize.
_HTR_STAGES = ("PageLoaderActor", "LayoutActor", "LineActor", "TranscribeViaServe", "AltoExportActor", "AltoWriterActor")
_PREFETCH_STAGES = ("PrefetchActor",)


def _slim_orchestrator_job(j: dict[str, Any]) -> dict[str, Any]:
    sid = j.get("submission_id") or ""
    m = _CHUNK_RE.search(sid)
    return {
        "submission_id": sid,
        "status": j.get("status"),
        "start_time": j.get("start_time"),
        "chunk_id": int(m.group(1)) if m else None,
    }


def _task_summary_for_job(driver_job_id: str, stage_names: tuple[str, ...]) -> list[dict[str, Any]]:
    """Per-stage Ray Data task counts for one job.

    Hits ``/api/v0/tasks/summarize`` filtered by ``job_id`` and pulls out
    the ``MapWorker(MapBatches(<Stage>)).submit`` rows — those are the
    actual per-block task invocations (the ``__init__`` /
    ``get_location`` rows are pool-bookkeeping).

    Returns a list ordered by ``stage_names`` so the UI can render in
    pipeline order without re-sorting.
    """
    import httpx as _httpx

    url = ray_dashboard._dashboard_url()
    try:
        with _httpx.Client(timeout=5.0) as c:
            r = c.get(
                f"{url}/api/v0/tasks/summarize",
                params={"filter_keys": "job_id", "filter_predicates": "=", "filter_values": driver_job_id},
            )
            r.raise_for_status()
            data = r.json()
    except Exception:
        return []
    summary = ((data.get("data") or {}).get("result") or {}).get("result", {}).get("node_id_to_summary", {}).get("cluster", {}).get("summary", {})
    out: list[dict[str, Any]] = []
    for stage in stage_names:
        key = f"MapWorker(MapBatches({stage})).submit"
        info = summary.get(key) or {}
        sc = info.get("state_counts") or {}
        # Collapse Ray's task states into the four buckets the UI cares about.
        # Anything starting with PENDING_* (PENDING_NODE_ASSIGNMENT,
        # PENDING_ARGS_AVAIL, PENDING_OBJ_STORE_MEM_AVAIL, …) is "waiting for
        # scheduling or inputs" — Ray Dashboard groups them similarly.
        finished = int(sc.get("FINISHED", 0))
        # Split executing vs scheduled-but-not-yet-executing so backpressure is visible:
        #   - running = task is actively executing on a worker
        #   - scheduled = task is assigned to a worker but hasn't started (queued at actor)
        # Ray Data's "Queued blocks" (waiting to BECOME tasks) are not visible here —
        # they're inside Ray Data's bundle queue, upstream of Ray's task scheduler.
        running = int(sc.get("RUNNING", 0))
        scheduled = int(sc.get("SUBMITTED_TO_WORKER", 0))
        failed = int(sc.get("FAILED", 0))
        pending = sum(int(v) for k, v in sc.items() if k.startswith("PENDING") or k == "WAITING")
        total = sum(int(v) for v in sc.values())
        out.append(
            {
                "stage": stage,
                "finished": finished,
                "running": running,
                "scheduled": scheduled,
                "pending": pending,
                "failed": failed,
                "total": total,
            }
        )
    return out


def _driver_job_id(submission_id: str) -> str | None:
    """Map a submission_id (e.g. 'htr-chunk-000-of-100') to Ray's internal job_id."""
    import httpx as _httpx

    url = ray_dashboard._dashboard_url()
    try:
        with _httpx.Client(timeout=3.0) as c:
            r = c.get(f"{url}/api/jobs/{submission_id}")
            r.raise_for_status()
            return (r.json() or {}).get("job_id")
    except Exception:
        return None


def _orchestrator_state() -> dict[str, Any]:
    """Derive what the orchestrator would do next from current Ray + DB state.

    No new tables; this is the same logic ``scripts/orchestrator.py`` runs
    in its tick, surfaced for the UI so users can see the slot state, the
    next chunks queued for each pipeline, and any chunks currently in
    failure-cooldown.
    """
    import time as _time

    if not _BATCHES_DB.exists():
        return {"ok": False, "error": "batches.db not built"}

    jobs_payload = ray_dashboard.list_jobs()
    if not jobs_payload.get("ok"):
        return {"ok": False, "error": jobs_payload.get("error", "ray dashboard unreachable")}
    jobs: list[dict[str, Any]] = jobs_payload.get("jobs") or []

    def running_for(prefix: str) -> dict[str, Any] | None:
        for j in jobs:
            sid = j.get("submission_id") or ""
            if sid.startswith(prefix) and j.get("status") in ("RUNNING", "PENDING"):
                return j
        return None

    prefetch_running = running_for("prefetch-")
    htr_running = running_for("htr-")

    # Failure cooldowns: FAILED jobs whose end_time was within the cooldown.
    # Ray timestamps are now in seconds (we convert at the API boundary).
    now = _time.time()
    cooldowns: list[dict[str, Any]] = []
    for j in jobs:
        if j.get("status") != "FAILED":
            continue
        end = j.get("end_time")
        if not end:
            continue
        elapsed = now - float(end)
        if elapsed < _FAIL_COOLDOWN_SECS:
            sid = j.get("submission_id") or ""
            m = _CHUNK_RE.search(sid)
            if not m:
                continue
            cooldowns.append(
                {
                    "submission_id": sid,
                    "chunk_id": int(m.group(1)),
                    "pipeline": "prefetch" if sid.startswith("prefetch-") else "htr",
                    "expires_in_secs": max(0, int(_FAIL_COOLDOWN_SECS - elapsed)),
                }
            )

    db = sqlite3.connect(_BATCHES_DB)
    try:
        prefetch_pending = [
            r[0]
            for r in db.execute(
                """SELECT chunk_id FROM batches
                   WHERE chunk_id IS NOT NULL
                     AND manifest_status = 'ok'
                     AND COALESCE(cached_pages, 0) < COALESCE(page_count, 0)
                   GROUP BY chunk_id ORDER BY chunk_id"""
            )
        ]
        ready_for_htr: list[int] = []
        for cid, expected, cached, transcribed in db.execute(
            """SELECT chunk_id,
                      COALESCE(SUM(page_count), 0),
                      COALESCE(SUM(cached_pages), 0),
                      COALESCE(SUM(transcribed_pages), 0)
                 FROM batches
                WHERE chunk_id IS NOT NULL AND manifest_status = 'ok'
                GROUP BY chunk_id ORDER BY chunk_id"""
        ):
            if not expected or transcribed >= expected:
                continue
            if cached / expected >= _HTR_READY_FRACTION:
                ready_for_htr.append(cid)
    finally:
        db.close()

    cooldown_pf = {c["chunk_id"] for c in cooldowns if c["pipeline"] == "prefetch"}
    cooldown_htr = {c["chunk_id"] for c in cooldowns if c["pipeline"] == "htr"}
    next_prefetch = next((cid for cid in prefetch_pending if cid not in cooldown_pf), None)
    next_htr = next((cid for cid in ready_for_htr if cid not in cooldown_htr), None)

    return {
        "ok": True,
        "prefetch": _build_slot(prefetch_running, next_prefetch, len(prefetch_pending), _PREFETCH_STAGES),
        "htr": _build_slot(htr_running, next_htr, len(ready_for_htr), _HTR_STAGES),
        "cooldowns": cooldowns,
        "ready_threshold": _HTR_READY_FRACTION,
        "cooldown_secs": _FAIL_COOLDOWN_SECS,
    }


def _build_slot(
    running: dict[str, Any] | None,
    next_chunk: int | None,
    queue_len: int,
    stages: tuple[str, ...],
) -> dict[str, Any]:
    slot: dict[str, Any] = {
        "running": _slim_orchestrator_job(running) if running else None,
        "next": next_chunk,
        "queue_len": queue_len,
        "stages": [],
    }
    if running:
        jid = _driver_job_id(running.get("submission_id") or "")
        if jid:
            slot["stages"] = _task_summary_for_job(jid, stages)
    return slot


def _register_orchestrator_routes(app: FastAPI) -> None:
    @app.get("/api/orchestrator/state")
    def orchestrator_state() -> dict:
        return _orchestrator_state()


def _local_batch_status(bild_ids: list[str]) -> tuple[set[str], set[str], set[str]]:
    """For a list of bild_ids, return (listed, cached, transcribed) sets.

    Three tiers, each strictly nested:

    - `listed`       = batch_id exists in batches.db (we know the manifest).
    - `cached`       = also has cached_pages > 0 (we've downloaded at least
                        one page from S3, so the viewer has something to show).
    - `transcribed`  = also has transcribed_pages > 0 (HTR has produced ALTO
                        for at least one page; the viewer can show text +
                        the line will appear in /api/search hits).

    The frontend can render escalating badges and route opens accordingly:
    transcribed → our viewer (with text), cached → our viewer (images only),
    listed-but-not-cached → Riksarkivet (queued for prefetch), none → also
    Riksarkivet.
    """
    if not bild_ids or not _BATCHES_DB.exists():
        return set(), set(), set()
    db = sqlite3.connect(_BATCHES_DB)
    try:
        placeholders = ",".join("?" * len(bild_ids))
        rows = db.execute(
            f"SELECT batch_id, COALESCE(cached_pages, 0), COALESCE(transcribed_pages, 0) FROM batches WHERE batch_id IN ({placeholders})",  # noqa: S608 — placeholders are literal "?"s
            bild_ids,
        ).fetchall()
        listed = {r[0] for r in rows}
        cached = {r[0] for r in rows if r[1] > 0}
        transcribed = {r[0] for r in rows if r[2] > 0}
        return listed, cached, transcribed
    finally:
        db.close()


def _register_search_routes(app: FastAPI) -> None:
    from viewer import search as _search

    @app.get("/api/search")
    def search_lines(q: str, limit: int = 50) -> dict:
        if limit < 1 or limit > 500:
            raise HTTPException(400, "limit must be in [1, 500]")
        hits = _search.search(q, limit=limit)
        return {"ok": True, "query": q, "count": len(hits), "hits": hits}

    @app.get("/api/search/stats")
    def search_stats() -> dict:
        return _search.stats()

    @app.get("/api/search/thumb/{thumb_path:path}")
    def search_thumb(thumb_path: str) -> Response:
        # `thumb_path` is the rest of the URL, e.g. "thumbs/A0060198/A0060198_00001.jpg/textline0.jpg".
        # The search.fetch_thumb checks the prefix is "thumbs/" so the proxy can't
        # be tricked into fetching arbitrary keys.
        data = _search.fetch_thumb(thumb_path)
        if data is None:
            raise HTTPException(404, "thumbnail not found")
        return Response(
            content=data,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )


def _register_catalog_routes(app: FastAPI) -> None:
    """EAD catalog routes — kept separate from the line-search routes so
    neither registration function trips ruff's McCabe complexity cap."""
    from viewer import search as _search

    @app.get("/api/catalog/search")
    def catalog_search(q: str, limit: int = 50) -> dict:
        if limit < 1 or limit > 500:
            raise HTTPException(400, "limit must be in [1, 500]")
        hits = _search.catalog_search(q, limit=limit)
        # Three nested flags per hit (transcribed ⊂ cached ⊂ listed):
        #   listed       = batch_id is in batches.db
        #   cached       = also has cached_pages > 0 (some images on disk)
        #   transcribed  = also has transcribed_pages > 0 (some ALTO produced)
        # Frontend uses these to choose between our viewer and Riksarkivet's
        # public viewer, and to render escalating progress badges.
        bild_ids = [h["bild_id"] for h in hits if h.get("bild_id")]
        listed, cached, transcribed = _local_batch_status(bild_ids)
        for h in hits:
            bid = h.get("bild_id")
            h["listed"] = bid in listed
            h["cached"] = bid in cached
            h["transcribed"] = bid in transcribed
        return {"ok": True, "query": q, "count": len(hits), "hits": hits}

    @app.get("/api/catalog/search/stats")
    def catalog_search_stats() -> dict:
        return _search.catalog_stats()

    @app.get("/api/batches/{batch_id}/catalog")
    def catalog_for_batch(batch_id: str) -> dict:
        # Direct lookup of the EAD row for a given batch (batch_id == bild_id).
        # 404 when the volume isn't in the harvested catalog (e.g. test
        # batches like A0060198 that aren't in OAI-PMH). The viewer treats
        # that as "no metadata panel" rather than an error.
        row = _search.catalog_by_bild_id(batch_id)
        if row is None:
            raise HTTPException(404, f"no catalog entry for {batch_id}")
        return row

    @app.get("/api/catalog/browse")
    def catalog_browse(tier: str = "cached", limit: int = 500, offset: int = 0) -> dict:
        # Browse mode: list every batch in batches.db at >= `tier`, enriched
        # with its EAD catalog row. Used by /browse page (no FTS query;
        # purely list-driven). Tiers nest: transcribed ⊂ cached ⊂ listed.
        if tier not in ("listed", "cached", "transcribed"):
            raise HTTPException(400, "tier must be listed | cached | transcribed")
        if limit < 1 or limit > 2000:
            raise HTTPException(400, "limit must be in [1, 2000]")
        if not _BATCHES_DB.exists():
            return {"ok": True, "tier": tier, "count": 0, "total": 0, "hits": []}
        if tier == "transcribed":
            where = "COALESCE(transcribed_pages, 0) > 0"
        elif tier == "cached":
            where = "COALESCE(cached_pages, 0) > 0"
        else:
            where = "1=1"
        db = sqlite3.connect(_BATCHES_DB)
        try:
            total = db.execute(f"SELECT COUNT(*) FROM batches WHERE {where}").fetchone()[0]  # noqa: S608
            rows = db.execute(
                f"SELECT batch_id, COALESCE(cached_pages, 0), COALESCE(transcribed_pages, 0) "  # noqa: S608
                f"FROM batches WHERE {where} ORDER BY batch_id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        finally:
            db.close()
        bild_ids = [r[0] for r in rows]
        cached = {r[0] for r in rows if r[1] > 0}
        transcribed = {r[0] for r in rows if r[2] > 0}
        catalog_rows = _search.catalog_by_bild_ids(bild_ids)
        hits: list[dict] = []
        # Walk the SQL-ordered list so the response order matches the DB
        # (batch_id alphabetic). Skipping bild_ids absent from the catalog
        # is fine — the Browse page is for archival exploration, those are
        # internal test batches with no archival context.
        for bid in bild_ids:
            row = catalog_rows.get(bid)
            if row is None:
                continue
            row["listed"] = True
            row["cached"] = bid in cached
            row["transcribed"] = bid in transcribed
            hits.append(row)
        return {"ok": True, "tier": tier, "count": len(hits), "total": total, "offset": offset, "hits": hits}


_RAY_PROXY_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"]


def _register_ray_routes(app: FastAPI) -> None:
    @app.get("/api/ray/health")
    def ray_health() -> dict:
        return ray_dashboard.health()

    @app.get("/api/ray/jobs")
    def ray_jobs() -> dict:
        return ray_dashboard.list_jobs()

    @app.get("/api/ray/cluster")
    def ray_cluster() -> dict:
        return ray_dashboard.cluster_status()

    # ---- same-origin proxy for the embedded /embed iframe ----
    # Ray Dashboard's SPA is served at :8265; we forward both the SPA shell
    # (under /ray-dashboard/*) AND the absolute API paths its bundled JS hits
    # (/api/v0/*, /api/jobs, /api/version, /api/cluster_status, /api/grafana_*,
    # /logs/*). The frontend iframe loads /ray-dashboard/, so the user only
    # needs the frontend port forwarded — no need to also expose :8265.
    async def _proxy(path: str, request: Request) -> Response:
        body = await request.body()
        content, status, hdrs = ray_dashboard.proxy(
            path,
            request.method,
            request.url.query,
            dict(request.headers),
            body,
        )
        return Response(content=content, status_code=status, headers=hdrs)

    # Ray Dashboard reverse-proxy routes — pure plumbing, not first-class APIs.
    # `include_in_schema=False` keeps them out of /api/openapi.json so the
    # Swagger page only shows viewer's own endpoints.
    @app.api_route("/ray-dashboard/{path:path}", methods=_RAY_PROXY_METHODS, include_in_schema=False)
    async def ray_dashboard_spa(path: str, request: Request) -> Response:
        return await _proxy(path, request)

    @app.api_route("/ray-dashboard", methods=["GET", "HEAD"], include_in_schema=False)
    async def ray_dashboard_root(request: Request) -> Response:
        return await _proxy("", request)

    # Absolute paths Ray's bundled JS calls. Each gets its own catch-all to keep
    # FastAPI's routing predictable; none of these collide with our /api/* tree
    # (which is only /api/health, /api/volumes, /api/batches, /api/chunks,
    # /api/ray).
    for prefix in ("/api/v0", "/api/jobs", "/logs"):

        @app.api_route(
            prefix + "/{path:path}",
            methods=_RAY_PROXY_METHODS,
            name=f"ray-proxy-{prefix}",
            include_in_schema=False,
        )
        async def _catchall(path: str, request: Request, _prefix: str = prefix) -> Response:
            return await _proxy(_prefix.lstrip("/") + "/" + path, request)

        @app.api_route(
            prefix,
            methods=["GET", "HEAD"],
            name=f"ray-proxy-{prefix}-root",
            include_in_schema=False,
        )
        async def _catchall_root(request: Request, _prefix: str = prefix) -> Response:
            return await _proxy(_prefix.lstrip("/"), request)

    for exact in (
        "/api/cluster_status",
        "/api/version",
        "/api/grafana_health",
        "/api/prometheus_health",
        "/api/authenticate",
        "/api/authentication_mode",
    ):

        @app.api_route(
            exact,
            methods=["GET", "POST", "HEAD"],
            name=f"ray-proxy-{exact}",
            include_in_schema=False,
        )
        async def _exact(request: Request, _path: str = exact.lstrip("/")) -> Response:
            return await _proxy(_path, request)


def _image_mime(key: str) -> str:
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "tif": "image/tiff",
        "tiff": "image/tiff",
    }.get(ext, "application/octet-stream")


app = create_app()
