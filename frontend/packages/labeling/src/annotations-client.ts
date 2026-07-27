/**
 * Annotations HTTP client + payload assembly — the write plane's transport, in plain
 * TS (framework-agnostic, same pattern as history.ts / jobs.ts / tag-writer.ts).
 *
 * The runes controller stays a pure state facade: it delegates every fetch and every
 * wire-shape decision here — load (Arrow IPC + version header), save (the batched
 * delta + 409 contract), and interactive AI-assist. The ONE place backend route
 * shapes are known on the client.
 */
import { type Table, tableFromIPC } from 'apache-arrow';

/** A new row queued for Save (backend `NewAnnotation`). Unit identity is stamped
 *  server-side, so only shape data + attributes travel. */
export interface InsertRow {
	id: string;
	shape_type: string;
	x: number;
	y: number;
	width: number;
	height: number;
	rotation: number;
	polygon: number[];
	t_start: number;
	t_end: number;
	mask: string;
	label: string;
	text: string;
	group: string;
	status: string;
	source: string;
}

/** The ONE InsertRow factory — human-drawn defaults; callers override what differs
 *  (a segment's times, a prediction's status/source/label). Replaces three drifting
 *  16-field literals. */
export function makeInsertRow(partial: Partial<InsertRow> & { shape_type: string }): InsertRow {
	return {
		id: crypto.randomUUID(),
		x: 0,
		y: 0,
		width: 0,
		height: 0,
		rotation: 0,
		polygon: [],
		t_start: 0,
		t_end: 0,
		mask: '',
		label: '',
		text: '',
		group: '',
		status: 'accepted',
		source: 'human',
		...partial,
	};
}

/** A predicted shape from the AI-assist endpoint (backend AssistShape). */
export interface AssistShape {
	shape_type?: string;
	x: number;
	y: number;
	width: number;
	height: number;
	polygon?: number[];
	label?: string;
	confidence?: number;
}

/** A non-2xx annotations GET — distinguishable from network/parse failures so callers
 *  can keep the old fetch semantics (status errors handled softly, throws propagated). */
export class AnnotationsHttpError extends Error {
	constructor(readonly status: number) {
		super(`annotations HTTP ${status}`);
	}
}

/** GET one unit's annotations: Arrow IPC + the Lance version (the optimistic-
 *  concurrency handshake). The shared load ritual for every viewer + reload.
 *  Throws {@link AnnotationsHttpError} on a non-2xx; network/parse errors throw raw. */
export async function loadAnnotations(url: string): Promise<{ table: Table; version: number }> {
	const res = await fetch(url);
	if (!res.ok) throw new AnnotationsHttpError(res.status);
	const version = Number(res.headers.get('X-Annotations-Version') ?? '0');
	const table = tableFromIPC(new Uint8Array(await res.arrayBuffer()));
	return { table, version };
}

/** The Save wire shape (backend `SaveAnnotations`). */
export interface SavePayload {
	edits: Array<{ id: string } & Record<string, string>>;
	inserts: InsertRow[];
	geometry: Array<{
		id: string;
		x: number;
		y: number;
		width: number;
		height: number;
		polygon: number[];
	}>;
	temporal: Array<{ id: string; t_start: number; t_end: number }>;
	deletes: string[];
	base_version: number | null;
}

/** Assemble the Save payload from the controller's overlays — pure. `resolveId` maps a
 *  row index to its stable annotation id (falls back to the index as a string). */
export function buildSavePayload(args: {
	overrides: ReadonlyMap<string, string>;
	geoEdits: ReadonlyMap<
		number,
		{ x: number; y: number; w: number; h: number; polygon?: number[] | null }
	>;
	temporalEdits: ReadonlyMap<number, { t_start: number; t_end: number }>;
	inserts: InsertRow[];
	deletes: string[];
	version: number | null;
	resolveId: (index: number) => string | null;
}): SavePayload {
	const id = (index: number) => args.resolveId(index) ?? String(index);

	const byIndex = new Map<number, Record<string, string>>();
	for (const [key, value] of args.overrides) {
		const sep = key.indexOf(':');
		const index = Number(key.slice(0, sep));
		const row = byIndex.get(index) ?? {};
		row[key.slice(sep + 1)] = value;
		byIndex.set(index, row);
	}
	return {
		edits: [...byIndex].map(([index, fields]) => ({ id: id(index), ...fields })),
		inserts: args.inserts,
		geometry: [...args.geoEdits].map(([index, g]) => ({
			id: id(index),
			x: g.x,
			y: g.y,
			width: g.w,
			height: g.h,
			polygon: g.polygon ?? [],
		})),
		temporal: [...args.temporalEdits].map(([index, s]) => ({
			id: id(index),
			t_start: s.t_start,
			t_end: s.t_end,
		})),
		deletes: args.deletes,
		base_version: args.version,
	};
}

export const payloadIsEmpty = (p: SavePayload): boolean =>
	p.edits.length === 0 &&
	p.inserts.length === 0 &&
	p.deletes.length === 0 &&
	p.geometry.length === 0 &&
	p.temporal.length === 0;

/** POST a Save. `conflict` = the table advanced under the client (HTTP 409). */
export async function postSave(
	url: string,
	payload: SavePayload,
): Promise<{ status: 'ok' | 'conflict' }> {
	const res = await fetch(url, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(payload),
	});
	if (res.status === 409) return { status: 'conflict' };
	if (!res.ok) throw new Error(`save failed (HTTP ${res.status})`);
	return { status: 'ok' };
}

/** The assist endpoint for a unit — derived from its annotations URL in ONE place. */
export const assistUrlFor = (annotationsUrl: string): string =>
	annotationsUrl.replace('/api/annotations/', '/api/assist/');

/** Run an interactive producer (GroundingDINO text / SAM region) over the unit. */
export async function requestAssist(
	assistUrl: string,
	body: {
		producer: string;
		prompt: string;
		region: { x: number; y: number; width: number; height: number } | null;
	},
): Promise<{ shapes: AssistShape[]; source: string }> {
	const res = await fetch(assistUrl, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify(body),
	});
	if (!res.ok) throw new Error(`assist failed (HTTP ${res.status})`);
	return (await res.json()) as { shapes: AssistShape[]; source: string };
}
