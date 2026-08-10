/**
 * The transcription LANE's reading model — which rows are lines of the document, in what order.
 *
 * The lane is the Label-Studio/CVAT composition applied to HTR: the page image on top, the
 * transcribed text as a first-class surface of the SAME workspace underneath — not a field in a
 * side panel. A "line" here is any row carrying text of its own (a span is a range into some
 * OTHER row's text, so `parentId` excludes it).
 *
 * Order: a DECLARED reading order first (the `order` attribute — a person or a model said "this
 * line is third", and marginalia/inserts make geometry lie), then top-to-bottom by the line's
 * position on the page (`y`), the way a person reads it — table order is write order, which for
 * model output is whatever the pipeline emitted. Rows without either keep table order, last.
 */
import type { AnnoRow } from './annotation-rows';

export interface TextLaneLine {
	index: number;
	id: string;
	label: string;
	text: string;
}

export function textLaneLines(rows: readonly AnnoRow[]): TextLaneLine[] {
	return rows
		.filter((r) => r.text !== '' && r.parentId === '')
		.sort((a, b) => {
			if (a.order != null && b.order != null && a.order !== b.order) return a.order - b.order;
			if (a.order != null && b.order == null) return -1;
			if (a.order == null && b.order != null) return 1;
			if (a.y != null && b.y != null && a.y !== b.y) return a.y - b.y;
			if (a.y != null && b.y == null) return -1;
			if (a.y == null && b.y != null) return 1;
			return a.index - b.index;
		})
		.map((r) => ({ index: r.index, id: r.id, label: r.label, text: r.text }));
}
