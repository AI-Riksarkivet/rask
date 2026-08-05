/**
 * The corpus browser's DOCUMENT columns — what a table of documents can sort and filter on.
 *
 * Reported: "why does browse corpus looks so fucking wierd? why does it show like gallery view
 * initally?"
 *
 * The row this surface is named after is "Bulk labeling" (#42, and the rail renamed it), and its job
 * is to find five hundred pages and send them. It opened on a document GALLERY — tiles built for
 * looking at one document at a time — so the workflow the surface exists for had to be done by eye,
 * one tile at a time, with no way to sort by anything or filter to a decade.
 *
 * The Data Manager shape already landed next door for the QUEUE (#58/#59): a table whose columns
 * come from the corpus itself, plus a grid you can toggle to. This is that same shape, one level up,
 * over documents instead of tasks — and it is deliberately the same code path, because two different
 * "table of corpus rows" implementations in one zone would drift the way the dock panels did.
 *
 * A document row is an arbitrary `Row`; what is DISPLAYABLE about it is whatever the descriptor
 * declares in `display.metadata`, which is exactly what the gallery's tile already prints as its
 * meta line. So the columns are not invented here — they are read from the corpus, the same way
 * `item-columns.ts` reads them for the queue.
 */

/** The subset of `DatasetView` this module needs. Structural, so a test does not have to build a
 *  whole descriptor to check column derivation. */
export interface DocView {
	docId: (row: Record<string, unknown>) => string;
	/** `DatasetView.title` — NOT `docTitle`.
	 *
	 *  The first cut named this `docTitle?`, which no corpus has, so the Title column could never
	 *  render anywhere. TypeScript accepted it because an OPTIONAL member that is absent still
	 *  satisfies the interface — a real `DatasetView` structurally matched a shape it did not
	 *  implement, and the unit tests passed because their fixture defined the invented name. Only
	 *  driving the page found it. Required now, so the same substitution cannot recur silently. */
	title: (row: Record<string, unknown>) => string;
	/** The title FIELDS the corpus declares.
	 *
	 *  Needed separately because `title()` falls back to the doc id when nothing is declared, so
	 *  calling it can never tell you whether a title exists — it would just print the id twice, in
	 *  two columns, which is exactly the useless column this check exists to drop. */
	titleFields: string[];
	metadataFields: { field: string; label: string }[];
}

export interface DocColumn {
	/** The row key this column reads. `__id` and `__title` are synthetic — see `docCellText`. */
	field: string;
	label: string;
}

/** The synthetic column ids, kept as constants so a caller cannot typo one into a silent blank. */
export const DOC_ID = '__id';
export const DOC_TITLE = '__title';

/**
 * The columns a document table shows: identity first, then whatever the corpus declares.
 *
 * IDENTITY LEADS and is not optional. The reported complaint about the queue was that its rows
 * showed "ZERO of the item's own columns"; the mirrored mistake here would be a table of metadata
 * with no way to tell which document a row IS. The title is dropped when the corpus declares no
 * title field — a column that is empty for every row is worse than an absent one, because it costs
 * width and implies the data is missing rather than undeclared.
 */
export function docColumns(view: DocView): DocColumn[] {
	const cols: DocColumn[] = [{ field: DOC_ID, label: 'Document' }];
	if (view.titleFields.length > 0) cols.push({ field: DOC_TITLE, label: 'Title' });
	// Declared order, not alphabetical: the descriptor's author chose it, and re-sorting would
	// silently overrule a decision made where the corpus is defined.
	for (const m of view.metadataFields) cols.push({ field: m.field, label: m.label });
	return cols;
}

/** One cell's text. Synthetic columns resolve through the view; everything else is a row lookup. */
export function docCellText(
	view: DocView,
	row: Record<string, unknown>,
	field: string,
): string {
	if (field === DOC_ID) return view.docId(row);
	if (field === DOC_TITLE) return view.titleFields.length > 0 ? view.title(row) : '';
	const value = row[field];
	if (value == null) return '';
	// Arrays render joined rather than as `[object Object]` — a repeated field (authors, subjects) is
	// common in archival metadata and is the case a naive `String(value)` gets visibly wrong.
	return Array.isArray(value) ? value.map(String).join(', ') : String(value);
}

/**
 * Does this document match a free-text filter?
 *
 * Matches across EVERY displayed column, not just the title. The filter's job on this surface is
 * "narrow five thousand documents to the ones I mean", and a person typing `1663` means the year
 * wherever it lives — restricting it to one column would answer a question nobody asked.
 */
export function matchesDoc(view: DocView, row: Record<string, unknown>, query: string): boolean {
	const q = query.trim().toLowerCase();
	if (!q) return true;
	return docColumns(view).some((c) => docCellText(view, row, c.field).toLowerCase().includes(q));
}

/** Compare two documents on one column, for a stable sort. */
export function compareDocs(
	view: DocView,
	a: Record<string, unknown>,
	b: Record<string, unknown>,
	field: string,
): number {
	const av = docCellText(view, a, field);
	const bv = docCellText(view, b, field);
	// Numeric when BOTH sides are numeric — otherwise `10` sorts before `9`.
	const an = Number(av);
	const bn = Number(bv);
	if (av !== '' && bv !== '' && !Number.isNaN(an) && !Number.isNaN(bn)) return an - bn;

	// `sv` EXPLICITLY, not the runtime default. This is the Swedish National Archives: Å, Ä and Ö are
	// the last three letters of the alphabet, and under an en-US collation they sort next to A and O
	// instead — so an archive list would be ordered wrongly for every person reading it, on a machine
	// whose locale nobody chose. The first cut left the locale off while its own comment claimed
	// Swedish ordering; the test caught the gap between the two.
	return av.localeCompare(bv, 'sv');
}
