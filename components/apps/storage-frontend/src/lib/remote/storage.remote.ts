import * as v from 'valibot';
import { error } from '@sveltejs/kit';
import { query } from '$app/server';
import { GATEWAY_URL } from '$lib/server/env';
import {
	BUCKETS,
	S3ListingSchema,
	S3ObjectHeadSchema,
	type S3Listing,
	type S3ObjectHead,
} from '$lib/storage';

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
		// Surface the real upstream status to the client (SvelteKit hides plain
		// throws as a generic "Internal Error"); 404 here usually means the
		// gateway/volumes-api is down or outdated (missing /objects).
		error(502, `volumes-api ${bucket}/${prefix || ''} → HTTP ${res.status} ${res.statusText}`);
	}
	// Parse the untrusted response at the boundary (parse-don't-validate).
	return v.parse(S3ListingSchema, await res.json());
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
		error(502, `volumes-api object ${bucket}/${key} → HTTP ${res.status} ${res.statusText}`);
	}
	return v.parse(S3ObjectHeadSchema, await res.json());
});
