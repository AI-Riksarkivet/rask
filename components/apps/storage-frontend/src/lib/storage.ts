import * as v from 'valibot';

/** A single object under a prefix (mirrors `S3Object` in volumes-api). */
const S3ObjectSchema = v.object({
	key: v.string(),
	size: v.number(),
	last_modified: v.nullable(v.string()),
});
export type S3Object = v.InferOutput<typeof S3ObjectSchema>;

/** A delimiter-listed page of a bucket (mirrors `S3Listing` in volumes-api). */
export const S3ListingSchema = v.object({
	bucket: v.string(),
	prefix: v.string(),
	/** "Folder" common-prefixes under `prefix` (delimiter `/`). */
	prefixes: v.array(v.string()),
	objects: v.array(S3ObjectSchema),
});
export type S3Listing = v.InferOutput<typeof S3ListingSchema>;

/** Single-object metadata (S3 HEAD) — mirrors `S3ObjectHead` in volumes-api. */
export const S3ObjectHeadSchema = v.object({
	key: v.string(),
	size: v.number(),
	content_type: v.nullable(v.string()),
	last_modified: v.nullable(v.string()),
	etag: v.nullable(v.string()),
});
export type S3ObjectHead = v.InferOutput<typeof S3ObjectHeadSchema>;

/** The two fixed rask buckets (input images + derived ALTO). */
export const BUCKETS = ['images-batch', 'images-batch-alto'] as const;
export type Bucket = (typeof BUCKETS)[number];
