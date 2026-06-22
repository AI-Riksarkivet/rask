import * as v from 'valibot';
import { query } from '$app/server';
import { GATEWAY_URL } from '$lib/server/env';
import { BUCKETS, type S3Listing } from '$lib/storage';

// ---------------------------------------------------------------------------
// First SvelteKit *remote function* in the app — the pattern the SSR migration
// is built around. `query()` runs ONLY on the SvelteKit Bun server, so it can
// reach the gateway via an absolute URL and never ships its body/secrets to the
// client. Args are validated with valibot (a Standard Schema, no zod). The UI
// calls `await listObjects({ bucket, prefix })` directly in markup.
//
// NOTE: `*.remote.ts` files may export ONLY remote functions — the S3 types and
// the BUCKETS const live in `$lib/storage`.
//
// Backend contract (NOT YET IMPLEMENTED — see docs/architecture/frontend-microfrontends.md):
//   GET /api/volumes/objects?bucket=<name>&prefix=<p>
//   -> { bucket, prefix, prefixes: string[], objects: { key, size, last_modified }[] }
// A thin delimiter-based `list_objects_v2` wrapper over `storage.s3_client` in
// volumes-api. Until it lands, this query surfaces a typed error the UI renders
// as an "endpoint pending" state.
// ---------------------------------------------------------------------------

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
