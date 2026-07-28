// Typed client for the media-plane VIEWER's object browser via this zone's /api/media BFF route
// (the R18 storage browser's data layer). The route forwards the path unchanged to the rask
// gateway, whose /api/media row routes it to the viewer (`/api/media/objects` → viewer
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

/** Every store the catalog knows, with its role. Reached through this zone's `/capi` proxy — the
 *  CATALOG's row. (`/api` is the lineage service; sending a catalog call there would 404 in a way
 *  that looks like a missing endpoint rather than a wrong upstream.) */
export const listStores = (): Promise<ApiResult<{ stores: Store[] }>> =>
	request<{ stores: Store[] }>('/capi', 'v1/stores');

/** The tier -> store view, grouped by the catalog. Derived there rather than transcribed here, so
 *  a store's tier cannot drift between the registry and the page that displays it. */
export const listStoresByTier = (): Promise<ApiResult<Record<string, Store[]>>> =>
	request<Record<string, Store[]>>('/capi', 'v1/stores/tiers');

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
	requestJSON<S3Listing>(`media/objects?${q({ bucket, prefix })}`);

/** Size / content-type / modified / etag for a single object (404 when it vanished). */
export const headObject = (bucket: Bucket, key: string): Promise<ApiResult<S3ObjectHead>> =>
	requestJSON<S3ObjectHead>(`media/object?${q({ bucket, key })}`);

/** The object byte proxy (download disposition). Doubles as the inline `<img src>`: a disposition
 *  header never stops an `<img>` fetch from rendering, and it covers BOTH buckets and any key
 *  shape. */
export const downloadUrl = (bucket: Bucket, key: string): string =>
	bffPath(`/api/media/object/download?${q({ bucket, key })}`);

/** The object's raw bytes — the text-preview read (errors keep the 404 ≠ 0 status split). */
export const fetchObjectBytes = (bucket: Bucket, key: string): Promise<ApiResult<ArrayBuffer>> =>
	binary('/api', `media/object/download?${q({ bucket, key })}`);

/** How the preview pane renders an object: inline image, decoded text, or metadata-only. */
export type PreviewKind = 'image' | 'text' | 'binary';

// Extension first (the buckets' keys are honest about their format), content-type as the fallback
// for extension-less keys. TIFF is deliberately NOT an inline image — browsers cannot render it.
const IMAGE_EXT = /\.(jpe?g|png|gif|webp|avif|svg)$/i;
const TEXT_EXT = /\.(xml|alto|json|jsonl|txt|md|csv|tsv|ya?ml|log)$/i;

export function previewKind(key: string, contentType: string | null): PreviewKind {
	if (IMAGE_EXT.test(key)) return 'image';
	if (TEXT_EXT.test(key)) return 'text';
	const ct = contentType ?? '';
	if (/^image\/(jpeg|png|gif|webp|avif|svg)/.test(ct)) return 'image';
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
