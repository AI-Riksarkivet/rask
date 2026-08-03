/**
 * Row[] → Apache Arrow IPC, for the two tabular BFF responses this zone serves.
 *
 * `api/atlas/chunks` returns up to **1000 full corpus rows per lasso selection** and `api/search` the
 * same row kind at n≤100 — the two payloads the transport ruling promotes off JSON. They are the shape
 * Arrow exists for: many rows, few columns, every column homogeneous. The zone already speaks this
 * dialect on the read side (`api/atlas/points` is Arrow end to end, decoded in `@rask/media-api` with
 * `tableFromIPC`), so this is the ENCODER half of an idiom the zone already had, not a new one.
 *
 * ## Why an encoder at all, when the upstream speaks JSON
 *
 * The viewer and search services answer `list[dict]`. Re-encoding costs one pass over the rows on the
 * zone server and buys a columnar, length-prefixed body on the hop that actually matters — zone → browser
 * — where a 1000-row selection was previously megabytes of repeated key names. The upstream hop is
 * in-cluster; the browser hop is not.
 *
 * ## The one thing a generic encoder has to get right
 *
 * A corpus row is NOT a fixed schema: `RowSchema` is a `looseObject` on purpose, and the columns are
 * whatever the dataset declares. So the schema is derived per response:
 *
 *   - a column whose non-null values are all strings / all numbers / all booleans becomes that Arrow type;
 *   - anything else — nested objects, lists (the `alignments` blob, `tags`), or a genuinely mixed column —
 *     is carried as its JSON text, and its NAME is recorded in the schema metadata under `json_columns`.
 *
 * The decoder reads that metadata and parses those columns back, so a round trip is lossless for every
 * shape the JSON body could carry. Recording the column names in the payload (rather than agreeing on a
 * fixed list) is what keeps the encoder corpus-agnostic: a dataset that adds a struct column needs no
 * change on either side.
 *
 * A column is rectangular, so a key absent from one row is that column's null in that row — and the
 * decoder therefore OMITS null cells rather than materialising them. That is not a shortcut: `RowSchema`
 * spells its envelope fields `v.optional(...)`, which admits `undefined` and not `null`, so a hit
 * without `_score` must come back WITHOUT `_score`, exactly as the JSON body had it. The one thing lost
 * is telling an explicit `null` from an absent key — a distinction the row envelope has never drawn
 * (every reader goes through `DatasetView`, which reads both as "no value").
 */
import type { RequestHandler } from '@sveltejs/kit';
import type { AuthLocals } from '@rask/api/bff';
import {
	Bool,
	Float64,
	Schema,
	Table,
	tableToIPC,
	vectorFromArray,
	Utf8,
	type Vector,
} from 'apache-arrow';

/** Schema-metadata key naming the columns whose values are JSON text. */
export const JSON_COLUMNS_KEY = 'json_columns';

/** The wire content type — the same one `api/atlas/points` already serves. */
const ARROW_STREAM_CONTENT_TYPE = 'application/vnd.apache.arrow.stream';

type Kind = 'string' | 'number' | 'boolean' | 'json' | 'empty';

/** What a single value contributes to its column's type decision. */
function kindOf(value: unknown): Kind {
	if (value === null || value === undefined) return 'empty';
	const t = typeof value;
	if (t === 'string') return 'string';
	if (t === 'number') return 'number';
	if (t === 'boolean') return 'boolean';
	return 'json';
}

/** A column is its values' single kind, or JSON text when they disagree (or are structured). */
function columnKind(values: readonly unknown[]): Kind {
	let kind: Kind = 'empty';
	for (const value of values) {
		const next = kindOf(value);
		if (next === 'empty') continue;
		if (next === 'json') return 'json';
		if (kind === 'empty') kind = next;
		else if (kind !== next) return 'json';
	}
	return kind;
}

/**
 * Encode rows as one Arrow IPC stream.
 *
 * Column order follows first appearance across the rows (not just the first row): a corpus row is a
 * loose object, and a column present only on later rows is still a column.
 */
export function rowsToArrowIPC(rows: readonly Record<string, unknown>[]): Uint8Array {
	const names: string[] = [];
	const seen = new Set<string>();
	for (const row of rows) {
		for (const name of Object.keys(row)) {
			if (!seen.has(name)) {
				seen.add(name);
				names.push(name);
			}
		}
	}

	const columns: Record<string, Vector> = {};
	const jsonColumns: string[] = [];
	for (const name of names) {
		const values = rows.map((row) => row[name] ?? null);
		const kind = columnKind(values);
		// The Arrow type is given EXPLICITLY rather than inferred: an all-null column has nothing to
		// infer from, and inference on a 1000-row column is a scan this already did once.
		if (kind === 'number') {
			columns[name] = vectorFromArray(values as (number | null)[], new Float64());
		} else if (kind === 'boolean') {
			columns[name] = vectorFromArray(values as (boolean | null)[], new Bool());
		} else if (kind === 'json') {
			jsonColumns.push(name);
			columns[name] = vectorFromArray(
				values.map((value) => (value === null ? null : JSON.stringify(value))),
				new Utf8(),
			);
		} else {
			// `string`, and `empty` (every value null) — the latter as a null Utf8 column rather than a
			// guessed type. The decoder hands back null for each, which is what the JSON body said.
			columns[name] = vectorFromArray(values as (string | null)[], new Utf8());
		}
	}

	// The decoder needs both: the row count (a table with no columns still has rows — an empty result,
	// or a corpus row that is all-null) and which columns to JSON-parse back.
	const metadata = new Map<string, string>([
		['rows', String(rows.length)],
		[JSON_COLUMNS_KEY, JSON.stringify(jsonColumns)],
	]);
	const table = names.length === 0 ? new Table(new Schema([], metadata)) : new Table(columns);
	if (names.length > 0) {
		for (const [key, value] of metadata) table.schema.metadata.set(key, value);
	}
	return tableToIPC(table, 'stream');
}

// ── The BFF half ───────────────────────────────────────────────────────────────────────────────────

const json = (body: unknown, status: number): Response =>
	new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

/**
 * A `+server.ts` handler that forwards to a media-plane service and serves its ROW array as Arrow.
 *
 * These two routes keep their `+server.ts` — `api/search`'s POST takes a multipart body (query by
 * example), which is a request shape a remote function cannot carry, and `api/atlas/chunks` is its
 * sibling read-spelled-as-a-POST. So the verdict is not "become a remote function" but "keep the route,
 * change the response", and everything else about them is preserved verbatim from the
 * `makeBackendProxy` they replace:
 *
 *   - the signed-in user's bearer, and nothing else, is forwarded;
 *   - `requireSession` fails closed on an auth-enabled stack BEFORE the request leaves the zone;
 *   - a non-2xx upstream answer passes through with its status and its JSON body, because the client
 *     parses `detail` out of it (`apiErrorFrom`) — only the SUCCESS body changes shape;
 *   - an unreachable upstream is a 502 with a JSON body, not a rejection, for the same reason.
 */
export function makeArrowRowsProxy(opts: {
	backendUrl: string;
	/** The upstream path, `/api/...` — these services serve their routes under `/api` unprefixed. */
	path: string;
	requireSession?: boolean | undefined;
}): RequestHandler {
	return async ({ url, fetch, request, locals }) => {
		const auth = locals as unknown as AuthLocals;
		if (opts.requireSession && auth.authEnabled && !auth.session) {
			return json({ detail: 'sign in required' }, 401);
		}
		const headers: Record<string, string> = {};
		if (auth.session) headers['authorization'] = `Bearer ${auth.session.accessToken}`;

		const isRead = request.method === 'GET' || request.method === 'HEAD';
		const init: RequestInit = isRead
			? { method: request.method, headers }
			: {
					method: request.method,
					headers: {
						...headers,
						'content-type': request.headers.get('content-type') ?? 'application/json',
					},
					// Bytes, not text: a multipart body (image search) is binary and a text round-trip
					// would corrupt it.
					body: await request.arrayBuffer(),
				};

		let upstream: Response;
		try {
			upstream = await fetch(`${opts.backendUrl}${opts.path}${url.search}`, init);
		} catch (err) {
			return json({ error: String(err) }, 502);
		}
		if (!upstream.ok) {
			return new Response(upstream.body, {
				status: upstream.status,
				headers: {
					'content-type': upstream.headers.get('content-type') ?? 'application/json',
				},
			});
		}
		let rows: unknown;
		try {
			rows = await upstream.json();
		} catch (err) {
			return json({ detail: `the upstream sent no JSON rows: ${String(err)}` }, 502);
		}
		if (!Array.isArray(rows)) {
			return json({ detail: 'the upstream sent a non-array row payload' }, 502);
		}
		// `.slice()` because Bun's Uint8Array here can sit on a SharedArrayBuffer-typed view, which the
		// DOM `BodyInit` union does not accept; the copy is one contiguous buffer.
		const bytes = rowsToArrowIPC(rows as Record<string, unknown>[]);
		return new Response(bytes.slice().buffer as ArrayBuffer, {
			headers: { 'content-type': ARROW_STREAM_CONTENT_TYPE },
		});
	};
}
