// Typed client for the rask VOLUMES service via this zone's /api/v1/volumes BFF route (the R18
// storage browser's data layer). The route forwards the path unchanged to the rask gateway, which
// path-routes /api/v1/volumes/* to volumes-api — shapes are hand-mirrored from
// services/volumes_api/schemas.py (no OpenAPI codegen exists for the rask fleet yet).
import {
	bffPath,
	requestBinary as binary,
	requestJSON as request,
	type ApiResult,
} from '$lib/http';

/** The two fixed rask buckets (input images + derived ALTO) — mirrors `Bucket` in the service. */
export const BUCKETS = ['images-batch', 'images-batch-alto'] as const;
export type Bucket = (typeof BUCKETS)[number];

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
	requestJSON<S3Listing>(`v1/volumes/objects?${q({ bucket, prefix })}`);

/** Size / content-type / modified / etag for a single object (404 when it vanished). */
export const headObject = (bucket: Bucket, key: string): Promise<ApiResult<S3ObjectHead>> =>
	requestJSON<S3ObjectHead>(`v1/volumes/object?${q({ bucket, key })}`);

/** The object byte proxy (download disposition). Doubles as the inline `<img src>`: a disposition
 *  header never stops an `<img>` fetch from rendering, and unlike the `/{vol}/pages/{key}/image`
 *  route this one covers BOTH buckets and any key shape. */
export const downloadUrl = (bucket: Bucket, key: string): string =>
	bffPath(`/api/v1/volumes/object/download?${q({ bucket, key })}`);

/** The object's raw bytes — the text-preview read (errors keep the 404 ≠ 0 status split). */
export const fetchObjectBytes = (bucket: Bucket, key: string): Promise<ApiResult<ArrayBuffer>> =>
	binary('/api', `v1/volumes/object/download?${q({ bucket, key })}`);

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
