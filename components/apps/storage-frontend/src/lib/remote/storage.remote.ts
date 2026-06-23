import * as v from 'valibot';
import { query } from '$app/server';
import { GATEWAY_URL } from '$lib/server/env';
import { BUCKETS, type S3Listing, type S3ObjectHead } from '$lib/storage';

// Remote functions (server-only) — the storage microfrontend's data layer over
// the gateway's agnostic volumes-api (works against MinIO/AWS/HCP, see
// docs/architecture/ra-hcp-migration.md). All read-only.

const ListArgs = v.object({
	bucket: v.picklist(BUCKETS),
	prefix: v.optional(v.string(), ''),
});

export const listObjects = query(ListArgs, async ({ bucket, prefix }): Promise<S3Listing> => {
	const url = new URL('/api/volumes/objects', GATEWAY_URL);
	url.searchParams.set('bucket', bucket);
	if (prefix) url.searchParams.set('prefix', prefix);

	const res = await fetch(url);
	if (!res.ok) {
		throw new Error(`listObjects(${bucket}/${prefix || ''}): HTTP ${res.status}`);
	}
	return res.json();
});

const ObjectArgs = v.object({
	bucket: v.picklist(BUCKETS),
	key: v.string(),
});

export const headObject = query(ObjectArgs, async ({ bucket, key }): Promise<S3ObjectHead> => {
	const url = new URL('/api/volumes/object', GATEWAY_URL);
	url.searchParams.set('bucket', bucket);
	url.searchParams.set('key', key);

	const res = await fetch(url);
	if (!res.ok) {
		throw new Error(`headObject(${bucket}/${key}): HTTP ${res.status}`);
	}
	return res.json();
});
