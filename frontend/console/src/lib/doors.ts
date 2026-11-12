/**
 * The console's door to the catalog: one call → the shared `ApiResult` union, the signed-in person's
 * bearer attached, the payload parsed at the wire boundary. The implementation is `#lib/api/client.ts`'s
 * `upstreamJSON`; what stays here is only the base each upstream answers at (`#lib/upstreams`).
 *
 * The semantics are the shared contract — 0 unreachable, n answered-n with the upstream's `{detail}`,
 * 502 contract drift.
 */
import type * as v from 'valibot';
import { parsed as sharedParsed, upstreamJSON, type ApiResult } from '#lib/api/client.ts';
import { CATALOG_API } from '#lib/upstreams.ts';

/** One catalog call → `ApiResult<unknown>`; the signed-in person's bearer rides along. */
export function catalogJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	return upstreamJSON({ base: CATALOG_API, path, init, upstream: 'catalog' });
}

/** Parse a successful wire payload; a shape drift is a 502-flavoured failure, never a cast. */
export function parsed<T>(
	result: ApiResult<unknown>,
	schema: v.GenericSchema<unknown, T>,
): ApiResult<T> {
	return sharedParsed(result, schema, 'catalog');
}

/** The rows one listing answers under its key: a list (tables, namespaces, versions, indexes) or a map
 *  keyed by name (tags, branches). */
type ListingRows = unknown[] | Record<string, unknown>;

function isRecord(value: unknown): value is Record<string, unknown> {
	return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** `rows` with `more` after them; `null` when the two are not the same kind of collection. A page
 *  answering no rows (an absent key: these replies drop empty fields) adds nothing. */
function joinRows(rows: ListingRows | undefined, more: unknown): ListingRows | undefined | null {
	if (more === undefined || more === null) return rows;
	if (Array.isArray(more))
		return rows === undefined || Array.isArray(rows) ? [...(rows ?? []), ...more] : null;
	if (isRecord(more)) return rows === undefined || isRecord(rows) ? { ...rows, ...more } : null;
	return null;
}

/**
 * Every page of a spec listing at `path`, its rows under `key` joined. The catalog answers a listing at
 * most a page of rows and a `page_token` that continues it (the spec's `PageToken`), whether or not the
 * caller names a `limit`, so a reader of the first page alone would show a listing cut short with
 * nothing saying so. The answer is the last page's body with every page's rows under `key` and no
 * `page_token`; a page that fails answers its failure, and a token that does not move is contract
 * drift (502), never a loop.
 */
export async function catalogListing(
	path: string,
	key: string,
	init?: RequestInit,
): Promise<ApiResult<Record<string, unknown>>> {
	const drift = (detail: string): ApiResult<Record<string, unknown>> => ({
		ok: false,
		status: 502,
		detail: `catalog listing ${path}: ${detail}`,
	});
	let rows: ListingRows | undefined;
	let token: string | undefined;
	for (;;) {
		const at =
			token === undefined
				? path
				: `${path}${path.includes('?') ? '&' : '?'}page_token=${encodeURIComponent(token)}`;
		const page = await catalogJSON(at, init);
		if (!page.ok) return page;
		if (!isRecord(page.data)) return drift('a page is not an object');
		const { page_token: next, ...body } = page.data;
		const joined = joinRows(rows, body[key]);
		if (joined === null) return drift(`the pages' ${key} are not one kind of collection`);
		rows = joined;
		if (typeof next !== 'string' || next === '') {
			return { ok: true, data: rows === undefined ? body : { ...body, [key]: rows } };
		}
		if (next === token) return drift(`page_token ${next} did not move`);
		token = next;
	}
}
