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
	group?: string | null;
	difficult?: boolean;
	source?: string | null;
	model_version?: string | null;
	confidence?: number | null;
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
const TEXTUAL = ['mask', 'label', 'text', 'group', 'source', 'model_version'] as const;

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
		const polygon = row['polygon'];
		if (polygon != null) {
			const values = Array.from(polygon as ArrayLike<number>, Number).filter(Number.isFinite);
			if (values.length > 0) shape.polygon = values;
		}
		shapes.push(shape);
	}
	return shapes;
}
