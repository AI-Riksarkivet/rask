/**
 * Span-offset remapping under a TEXT EDIT — the textual facet's integrity rule.
 *
 * A span is a `[start, end)` range into its parent's text. Correcting the transcription shifts
 * that text under every span anchored to it: without remapping, fixing one typo at the start of
 * a line silently re-points every entity after it at the wrong characters — annotations corrupted
 * by the act of improving the data they annotate.
 *
 * The edit is modelled as ONE replaced region, recovered from the old/new strings by longest
 * common prefix + suffix (an input event is a single insertion/deletion/replacement — IME bursts
 * included). Per span:
 *
 *  - entirely BEFORE the region → untouched;
 *  - entirely AFTER → shifted by the length delta;
 *  - the region strictly INSIDE the span → the span stretches with its text;
 *  - PARTIAL overlap → dropped, same stance as the renderer: the anchored text no longer exists,
 *    and a clamped guess would keep a confident-looking span on characters nobody labelled.
 */

export interface SpanRange {
	index: number;
	start: number;
	end: number;
}

export interface RemappedSpan {
	index: number;
	/** Null ⇒ the span's anchored text was edited away — delete it rather than guess. */
	start: number | null;
	end: number | null;
}

/** The single replaced region between two versions of a string: `[from, toOld)` became
 *  `[from, toNew)`. Equal strings yield an empty region at the end. */
export function editedRegion(
	oldText: string,
	newText: string,
): { from: number; toOld: number; toNew: number } {
	let p = 0;
	const maxP = Math.min(oldText.length, newText.length);
	while (p < maxP && oldText[p] === newText[p]) p++;
	let s = 0;
	while (s < maxP - p && oldText[oldText.length - 1 - s] === newText[newText.length - 1 - s]) s++;
	return { from: p, toOld: oldText.length - s, toNew: newText.length - s };
}

/** Remap every span for the `oldText → newText` edit. Pure; the caller applies the results. */
export function remapSpans(
	oldText: string,
	newText: string,
	spans: readonly SpanRange[],
): RemappedSpan[] {
	const { from, toOld, toNew } = editedRegion(oldText, newText);
	const delta = toNew - toOld;
	if (delta === 0 && from === toOld) return spans.map((s) => ({ ...s }));
	return spans.map(({ index, start, end }) => {
		if (end <= from) return { index, start, end }; // before the edit
		if (start >= toOld) return { index, start: start + delta, end: end + delta }; // after
		if (start <= from && end >= toOld) return { index, start, end: end + delta }; // contains it
		return { index, start: null, end: null }; // partially edited away — drop, never guess
	});
}
