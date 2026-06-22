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

/** The two fixed rask buckets (input images + derived ALTO). */
export const BUCKETS = ['images-batch', 'images-batch-alto'] as const;
export type Bucket = (typeof BUCKETS)[number];
