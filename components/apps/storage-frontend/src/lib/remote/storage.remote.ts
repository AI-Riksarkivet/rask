import * as v from 'valibot';
import { query } from '$app/server';
import { GATEWAY_URL } from '$lib/server/env';
import { BUCKETS, type S3Listing } from '$lib/storage';

// Remote function (server-only). See the monolith's copy for the full backend
// contract; this is the storage microfrontend's own data layer.

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
