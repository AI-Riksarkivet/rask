// Typed client for the media-plane VIEWER's object browser via this zone's /api/explorer BFF route
// (the R18 storage browser's data layer). The route forwards the path unchanged to the rask
// gateway, whose /api/explorer row routes it to the viewer (`/api/explorer/objects` → viewer
// `/api/objects`; the R6/R20 wave retired volumes-api). Shapes are hand-mirrored from
// services/viewer/src/viewer/api/v1/endpoints/objects.py (no OpenAPI codegen for the fleet yet).
import {
	bffPath,
	requestBinary as binary,
	requestJSON as request,
	type ApiResult,
} from '$lib/http';

/** Where a store sits in the cascade. Mirrors `StorageRole` in service_kit.schemas.storage; the
 *  three GOVERNED tiers are exactly bronze/silver/gold (R23) — raw is the external world, never a
 *  tier, and derived/observability sit alongside the medallion rather than inside it. */
export type StorageRole = 'raw' | 'bronze' | 'silver' | 'gold' | 'derived' | 'observability';

/** One registered object store (mirrors `Store`). */
export type Store = {
	name: string;
	bucket: string;
	role: StorageRole;
	description: string;
	read_only: boolean;
};

/** A store NAME. This replaced `BUCKETS`, a hardcoded two-value const that duplicated a Python
 *  `Literal` union by hand — two copies of one fact in two languages, kept in step by discipline,
 *  neither saying what either bucket was FOR. The registry is now fetched, so registering a store
 *  is config rather than a code change in both languages. */
export type Bucket = string;

/** What the attach form collects. `read_only` is absent on purpose — the catalog forces it true, so
 *  offering the choice here would be a control that does nothing.
 *
 *  The stores TRANSPORT (list / tiers / attach) lives in `remote/storage.remote.ts` — the zone's
 *  remote-function dialect. This module keeps the media-browser client (its route is keep-bytes:
 *  hrefs and content-disposition ARE the contract) plus the shared types and formatters. */
export type StoreDraft = {
	name: string;
	bucket: string;
	role: StorageRole;
	endpoint: string | null;
	description: string;
};

/** One object under a prefix (mirrors `S3Object`). */
export type S3Object = { key: string; size: number; last_modified: string | null };

/** One delimiter-listed level of a bucket (mirrors `S3Listing`): `prefixes` are the "folder"
 *  common-prefixes directly under `prefix`; `objects` are the leaf keys at this level. */
export type S3Listing = { bucket: string; prefix: string; prefixes: string[]; objects: S3Object[] };

/** Metadata for a single object — S3 HEAD (mirrors `S3ObjectHead`). */
export type S3ObjectHead = {
	key: string;
	size: number;
	content_type: string | null;
	last_modified: string | null;
	etag: string | null;
};

const requestJSON = <T>(path: string, init?: RequestInit) => request<T>('/api', path, init);

const q = (params: Record<string, string>): string => new URLSearchParams(params).toString();

/** One delimiter-scoped level of `bucket`/`prefix` for the object browser. */
export const listObjects = (bucket: Bucket, prefix: string): Promise<ApiResult<S3Listing>> =>
	requestJSON<S3Listing>(`explorer/objects?${q({ bucket, prefix })}`);

/** Size / content-type / modified / etag for a single object (404 when it vanished). */
export const headObject = (bucket: Bucket, key: string): Promise<ApiResult<S3ObjectHead>> =>
	requestJSON<S3ObjectHead>(`explorer/object?${q({ bucket, key })}`);

/** The object byte proxy (download disposition). Doubles as the inline `<img src>`: a disposition
 *  header never stops an `<img>` fetch from rendering, and it covers BOTH buckets and any key
 *  shape. */
export const downloadUrl = (bucket: Bucket, key: string): string =>
	bffPath(`/api/explorer/object/download?${q({ bucket, key })}`);

/** The object's raw bytes — the text-preview read (errors keep the 404 ≠ 0 status split). */
export const fetchObjectBytes = (bucket: Bucket, key: string): Promise<ApiResult<ArrayBuffer>> =>
	binary('/api', `explorer/object/download?${q({ bucket, key })}`);

/** How the preview pane renders an object: inline image, decoded text, or metadata-only. */
export type PreviewKind = 'image' | 'text' | 'video' | 'audio' | 'pdf' | 'binary';

// Extension first (the buckets' keys are honest about their format), content-type as the fallback
// for extension-less keys. TIFF is deliberately NOT an inline image — browsers cannot render it.
const IMAGE_EXT = /\.(jpe?g|png|gif|webp|avif|svg)$/i;
const TEXT_EXT = /\.(xml|alto|json|jsonl|txt|md|csv|tsv|ya?ml|log)$/i;
// Only what a browser can actually play with no plugin and no transcode. `.mkv` and `.avi` are
// deliberately absent: they are containers a browser will refuse or half-play, and a dead <video>
// element is a worse answer than an honest download link.
const VIDEO_EXT = /\.(mp4|webm|ogv|m4v|mov)$/i;
const AUDIO_EXT = /\.(mp3|wav|ogg|oga|m4a|flac|aac)$/i;
const PDF_EXT = /\.pdf$/i;

export function previewKind(key: string, contentType: string | null): PreviewKind {
	if (IMAGE_EXT.test(key)) return 'image';
	if (TEXT_EXT.test(key)) return 'text';
	if (VIDEO_EXT.test(key)) return 'video';
	if (AUDIO_EXT.test(key)) return 'audio';
	if (PDF_EXT.test(key)) return 'pdf';
	const ct = contentType ?? '';
	if (/^image\/(jpeg|png|gif|webp|avif|svg)/.test(ct)) return 'image';
	if (ct.startsWith('video/')) return 'video';
	if (ct.startsWith('audio/')) return 'audio';
	if (ct === 'application/pdf') return 'pdf';
	if (ct.startsWith('text/') || ct.includes('xml') || ct.includes('json')) return 'text';
	return 'binary';
}

/** Human object size — bytes up to whole-unit TB, one decimal under 10. */
export function fmtSize(bytes: number): string {
	if (bytes < 1024) return `${bytes} B`;
	const units = ['kB', 'MB', 'GB', 'TB'];
	let value = bytes;
	let unit = -1;
	do {
		value /= 1024;
		unit += 1;
	} while (value >= 1024 && unit < units.length - 1);
	return `${value >= 10 ? Math.round(value) : Number(value.toFixed(1))} ${units[unit] ?? 'TB'}`;
}

/** `2026-07-27T10:00:00+00:00` → `2026-07-27 10:00:00` — a string trim, no Date parsing to drift. */
export function fmtModified(iso: string | null): string {
	return iso === null ? '—' : iso.slice(0, 19).replace('T', ' ');
}

// ── Bronze page datasets (the document viewer) ────────────────────────────────────────────────
// A Lance dataset is a DIRECTORY of objects in a store, so the object browser walks into it and
// shows `.lance` fragments and a `_versions` manifest — bytes, not pages. These read the dataset
// as what it is: rows with a blob-v2 image column, served page-at-a-time by the viewer.

/** One page's metadata. `has_image` is false when the harvest produced no payload for that row —
 *  surfaced rather than hidden, because a viewer that silently skips a failed page reports a volume
 *  as complete when it is not. */
export type Page = {
	id: number;
	source_uri: string;
	stage: string;
	size: number;
	has_image: boolean;
};

export type PageListing = { dataset: string; pages: Page[] };

/** Page metadata for a catalog TABLE (never the bytes — a listing that inlined them would move
 *  hundreds of MB to draw a contact sheet).
 *
 *  Addressed by table id (`bronze$pages`), not by an s3:// URI. A URI parameter would let any caller
 *  point the viewer at any bucket its credentials reach, and would let this page render a dataset
 *  the catalog has never heard of — the viewer and the catalog disagreeing by construction. */
export const listPages = (table: string): Promise<ApiResult<PageListing>> =>
	request<PageListing>('/api', `explorer/pages?${q({ table })}`);

/** One page's image bytes, as an `<img src>`. Selected by the `id` COLUMN, never by row position —
 *  positional indexing into a blob read is the misattribution bug the read path exists to avoid. */
export const pageImageUrl = (table: string, id: number): string =>
	bffPath(`/api/explorer/page?${q({ table, id: String(id) })}`);

/** Does this browser location look like a Lance dataset rather than a folder of loose objects?
 *  Lance writes a `_versions/` manifest directory and `.lance` fragments; either is conclusive. */
export function looksLikeLanceDataset(prefixes: string[], objects: { key: string }[]): boolean {
	return (
		prefixes.some((p) => p.endsWith('_versions/') || p.endsWith('data/')) ||
		objects.some((o) => o.key.endsWith('.lance') || o.key.includes('/_versions/'))
	);
}

/** The catalog table id for a browser location, or null when the path is not namespace-shaped.
 *
 *  TIERS ARE NAMESPACES, so a registered table sits at `<namespace>/<table>/` inside the warehouse
 *  bucket and its catalog id is `<namespace>$<table>`. A dataset found anywhere else is real storage
 *  but NOT a registered table — the viewer refuses it rather than reading round the catalog, which
 *  is what "governed" has to mean if it means anything. */
export function tableIdForPrefix(prefix: string): string | null {
	const segs = prefix.split('/').filter(Boolean);
	return segs.length === 2 ? `${segs[0]}$${segs[1]}` : null;
}
