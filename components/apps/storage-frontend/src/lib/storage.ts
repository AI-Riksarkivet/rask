export type S3Object = {
	key: string;
	size: number;
	last_modified: string | null;
};

export type S3Listing = {
	bucket: string;
	prefix: string;
	/** "Folder" common-prefixes under `prefix` (delimiter `/`). */
	prefixes: string[];
	objects: S3Object[];
};

/** Single-object metadata (S3 HEAD) — mirrors `S3ObjectHead` in volumes-api. */
export type S3ObjectHead = {
	key: string;
	size: number;
	content_type: string | null;
	last_modified: string | null;
	etag: string | null;
};

/** The two fixed rask buckets (input images + derived ALTO). */
export const BUCKETS = ['images-batch', 'images-batch-alto'] as const;
export type Bucket = (typeof BUCKETS)[number];
