import * as v from 'valibot';
import { error } from '@sveltejs/kit';
import { query, getRequestEvent } from '$app/server';
import {
	BUCKETS,
	S3ListingSchema,
	S3ObjectHeadSchema,
	type S3Listing,
	type S3ObjectHead,
} from '$lib/storage';

// Remote functions (server-only) — the storage microfrontend's data layer over the
// gateway's agnostic volumes-api (any S3 backend: MinIO/rustfs/AWS). Read-only.
//
// SAME canonical pattern as the other data apps (overview/compute/discover): fetch
// RELATIVE `/api/*` via getRequestEvent().fetch; the per-app src/hooks.server.ts
// (makeGatewayHandleFetch) routes the SSR fetch to the in-cluster gateway. The
// volumes endpoints aren't in @rask/api, so the untrusted response is parsed here
// against local valibot schemas (parse-don't-validate), and error(502) surfaces the
// real upstream status (a plain throw becomes a generic "Internal Error").

const ListArgs = v.object({
	bucket: v.picklist(BUCKETS),
	prefix: v.optional(v.string(), ''),
});

export const listObjects = query(ListArgs, async ({ bucket, prefix }): Promise<S3Listing> => {
	const params = new URLSearchParams({ bucket });
	if (prefix) params.set('prefix', prefix);
	const res = await getRequestEvent().fetch(`/api/volumes/objects?${params}`);
	if (!res.ok) {
		// 404 here usually means the gateway/volumes-api is down or outdated (missing /objects).
		error(502, `volumes-api ${bucket}/${prefix || ''} → HTTP ${res.status} ${res.statusText}`);
	}
	return v.parse(S3ListingSchema, await res.json());
});

const ObjectArgs = v.object({
	bucket: v.picklist(BUCKETS),
	key: v.string(),
});

export const headObject = query(ObjectArgs, async ({ bucket, key }): Promise<S3ObjectHead> => {
	const params = new URLSearchParams({ bucket, key });
	const res = await getRequestEvent().fetch(`/api/volumes/object?${params}`);
	if (!res.ok) {
		error(502, `volumes-api object ${bucket}/${key} → HTTP ${res.status} ${res.statusText}`);
	}
	return v.parse(S3ObjectHeadSchema, await res.json());
});
