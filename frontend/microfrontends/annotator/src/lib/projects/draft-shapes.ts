/**
 * The pure canvas-row → task-draft-shape mapping (S10 first half). Kept free of any
 * SvelteKit import so it unit-tests without the app runtime; the transport half lives in
 * draft-sync.ts.
 */
import type { Table } from 'apache-arrow';
import { canonicalShapeType } from '@rask/labeling/shape-types';

/** One draft shape, in the task-draft model's wire shape (annotator.projects.models.Shape). */
export type DraftShape = {
	shape_id: string;
	shape_type: string;
	x?: number | null;
	y?: number | null;
	width?: number | null;
	height?: number | null;
	rotation?: number | null;
	polygon?: number[];
	t_start?: number | null;
	t_end?: number | null;
	mask?: string | null;
	label?: string | null;
	text?: string | null;
	parent_id?: string | null;
	char_start?: number;
	char_end?: number;
	group?: string | null;
	difficult?: boolean;
	source?: string | null;
	model_version?: string | null;
	confidence?: number | null;
	/** The ontology's typed per-class attributes, as string values — parsed out of the row's
	 *  `metadata` JSON so the submit validator judges what the annotator actually entered. */
	attributes?: Record<string, string>;
};

const NUMERIC = [
	'x',
	'y',
	'width',
	'height',
	'rotation',
	't_start',
	't_end',
	'confidence',
] as const;
const TEXTUAL = ['mask', 'label', 'text', 'group', 'source', 'model_version', 'parent_id'] as const;
/** The textual facet's range. Separate from NUMERIC because 0 is a MEANINGFUL char offset — the very
 *  first character — and a truthiness check would drop it, silently shifting every span that starts
 *  at the beginning of its line. */
const SPAN_OFFSETS = ['char_start', 'char_end'] as const;

/** Map the unit's annotation rows (the media plane's EMPTY_SCHEMA columns) onto draft shapes.
 *
 *  The two vocabularies were NOT aligned, despite this comment previously claiming they were: the
 *  engine names shapes after its tools (`rect`, `point`, `line`) and the service types the draft
 *  against `bbox | polygon | ...`, so only polygon and mask ever survived. Rows are normalized
 *  through the one seam on the way in as well as out, because rows already WRITTEN by the old
 *  canvas hold `rectangle` — a name neither side accepts — and a re-open must not silently drop
 *  them. Rows without a shape_type (pure tag rows etc.) are skipped rather than fabricated. */
export function rowsToShapes(table: Table): DraftShape[] {
	const shapes: DraftShape[] = [];
	for (let i = 0; i < table.numRows; i += 1) {
		const row = table.get(i) as Record<string, unknown> | null;
		if (!row) continue;
		const raw = row['shape_type'];
		if (typeof raw !== 'string' || !raw) continue;
		const shapeType = canonicalShapeType(raw);
		if (!shapeType) continue;
		const shape: DraftShape = {
			shape_id: String(row['id'] ?? `row-${i}`),
			shape_type: shapeType,
			difficult: Boolean(row['difficult']),
		};
		for (const field of NUMERIC) {
			const value = row[field];
			if (typeof value === 'number' && Number.isFinite(value)) shape[field] = value;
		}
		for (const field of TEXTUAL) {
			const value = row[field];
			if (typeof value === 'string' && value) shape[field] = value;
		}
		for (const field of SPAN_OFFSETS) {
			const value = row[field];
			// `Number.isInteger`, not truthiness: char 0 is the first character, not "absent".
			if (typeof value === 'number' && Number.isInteger(value)) shape[field] = value;
		}
		const polygon = row['polygon'];
		if (polygon != null) {
			const values = Array.from(polygon as ArrayLike<number>, Number).filter(Number.isFinite);
			if (values.length > 0) shape.polygon = values;
		}
		// Attributes ride the `metadata` JSON column; the draft (and so the submit validator)
		// gets them as the flat string map the Shape model declares. Malformed JSON is skipped
		// rather than fatal — one bad row must not sink the whole draft snapshot.
		const metadata = row['metadata'];
		if (typeof metadata === 'string' && metadata && metadata !== '{}') {
			try {
				const parsed = JSON.parse(metadata) as Record<string, unknown>;
				const entries = Object.entries(parsed).map(([k, v]) => [k, String(v)] as const);
				if (entries.length > 0) shape.attributes = Object.fromEntries(entries);
			} catch {
				// leave attributes unset
			}
		}
		shapes.push(shape);
	}
	return shapes;
}
