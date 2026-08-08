/**
 * Text-span RENDERING model — pure functions from (text, span rows) to the segments a
 * doccano-style surface paints.
 *
 * The interaction this feeds: the text itself is the annotation surface. Spans render as
 * class-coloured inline highlights with a label tag where each span ends; selecting text in the
 * surface labels a new span in place. That replaces the first cut of span tooling — a raw
 * `<input>` whose selection offsets fed a button beside it — which carried the right DATA and the
 * wrong SURFACE: nothing showed where existing spans sat, overlaps were invisible, and reading a
 * transcription meant reading it inside a form field.
 *
 * No DOM here: boundaries, coverage and colours are all checkable without a browser. The component
 * owns only selection capture and clicks.
 */

/** One span row, projected for rendering — offsets into the parent row's `text`. */
export interface SpanMark {
	/** The span's OWN row index (selection / deletion target). */
	index: number;
	id: string;
	start: number;
	end: number;
	label: string;
}

/** A run of text under a constant set of covering spans. */
export interface Segment {
	start: number;
	end: number;
	text: string;
	/** Spans covering this whole segment, outermost (earliest start) first. */
	marks: SpanMark[];
	/** Marks whose span ENDS exactly here — where the surface renders their label tag. */
	ending: SpanMark[];
}

/**
 * Cut `text` at every span boundary. Each returned segment is covered by a constant set of spans,
 * so overlapping spans come out as stacked coverage rather than broken ranges — the classic
 * "Gustaf Eriksson Vasa" person-span overlapping a "Vasa" dynasty-span renders as three segments
 * with coverage 1·1·2, never as a corrupted range.
 *
 * Invalid spans are DROPPED, not clamped into meaning something else: a span pointing outside the
 * text no longer describes this text (it was authored against another revision), and rendering a
 * clamped version would silently show a different annotation than the row states.
 */
export function segmentText(text: string, marks: readonly SpanMark[]): Segment[] {
	const valid = marks.filter((m) => m.start >= 0 && m.end > m.start && m.end <= text.length);
	const cuts = new Set<number>([0, text.length]);
	for (const m of valid) {
		cuts.add(m.start);
		cuts.add(m.end);
	}
	const bounds = [...cuts].sort((a, b) => a - b);
	const out: Segment[] = [];
	for (let i = 0; i < bounds.length - 1; i++) {
		const start = bounds[i]!;
		const end = bounds[i + 1]!;
		if (end <= start) continue;
		const covering = valid
			.filter((m) => m.start <= start && m.end >= end)
			.sort((a, b) => a.start - b.start || b.end - a.end);
		out.push({
			start,
			end,
			text: text.slice(start, end),
			marks: covering,
			ending: valid.filter((m) => m.end === end),
		});
	}
	return out;
}

/**
 * Deterministic class → colour. A small OKLCH wheel with steady lightness/chroma so every hue
 * holds the same weight on both themes; assignment is by POSITION in the declared class list
 * (stable within a task), with a name-hash fallback for a label outside the taxonomy, so the
 * same label never flickers between colours across renders.
 */
const WHEEL = [
	'oklch(0.62 0.17 255)', // blue
	'oklch(0.65 0.15 150)', // green
	'oklch(0.68 0.15 65)', // amber
	'oklch(0.62 0.19 20)', // red
	'oklch(0.62 0.17 305)', // purple
	'oklch(0.68 0.13 195)', // teal
	'oklch(0.64 0.17 340)', // magenta
	'oklch(0.66 0.13 110)', // olive
] as const;

export function spanColor(label: string, classes: readonly string[]): string {
	const at = classes.indexOf(label);
	if (at >= 0) return WHEEL[at % WHEEL.length]!;
	let h = 0;
	for (let i = 0; i < label.length; i++) h = (h * 31 + label.charCodeAt(i)) >>> 0;
	return WHEEL[h % WHEEL.length]!;
}
