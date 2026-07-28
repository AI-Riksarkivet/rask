# G7 — the document viewer (provenance note)

The implementation landed in commit `cb781cd`, whose message is about a docs
correction. That commit used a broad `git add` from a concurrent session and swept up
work in progress from another one, so `git log` attributes the viewer to a docs sweep.
The code is correct and pushed; only the attribution is wrong, and `cb781cd` was already
on the remote when this was noticed, so rewriting it was not safe.

What actually landed there, and why it looks the way it does:

- `services/viewer/src/viewer/api/v1/endpoints/pages.py` — `GET /api/pages` (metadata
  only) and `GET /api/page` (image bytes) over a bronze blob-v2 page dataset.
- `frontend/microfrontends/lakehouse/src/lib/storage/PagePreview.svelte` — the page panel.
- `ObjectBrowser.svelte` — detects a Lance dataset (`_versions/` or `data/` prefix) and
  swaps the byte-preview for that panel.
- `storage.ts` — `listPages`, `pageImageUrl`, `looksLikeLanceDataset`.

The one invariant worth carrying forward: **reads must preserve cardinality.**
`read_blobs`/`take_blobs` silently drop null rows, so pairing their output positionally
against a scan of the tabular columns misattributes every page after the first gap, with
no exception raised — a failed harvest is exactly that case. The endpoints therefore read
through `service_kit.lakehouse.blobs.read_aligned_table` (`blob_handling="all_binary"`),
and `/api/page` selects by the `id` COLUMN rather than row position.

Witnessed 2026-07-28, RustFS → viewer → gateway → zone BFF → browser:

```
GET /api/pages      -> 3 pages, real source_uris, has_image true
GET /api/page?id=1  -> HTTP 200 image/jpeg 915081 bytes, JPEG 7096x5272 300dpi
browser             -> natural sizes 3507x2480, 7096x5272, 3096x4872
rendered            -> "TROLLDOM OCH ANNAN VIDSKEPELSE", A0060198, 1757,
                       Riksarkivet/SVAR — a real archival scan, not a placeholder
```
