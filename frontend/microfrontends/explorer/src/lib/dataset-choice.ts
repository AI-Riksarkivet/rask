/**
 * Choosing which corpus the zone is looking at.
 *
 * The MECHANISM already existed and was invisible: `descriptor-store` reads `?dataset=<id>` and
 * loads that corpus, with no param meaning the backend's default DB. So the zone could always serve
 * any dataset — there was simply no way to say which one you wanted without hand-editing the URL.
 * The backend has listed them at `GET /api/datasets` the whole time.
 *
 * The URL stays the source of truth rather than a store: it is what `descriptor-store` already
 * reads, it survives a reload, and it is what someone pastes into chat when they want a colleague to
 * see the same thing. A second in-memory notion of "the current dataset" could disagree with the one
 * the descriptor actually loaded, and that disagreement would be silent.
 *
 * Pure and separate from any component so "which URL does picking X produce, and which entry is
 * selected" is answerable without a browser.
 */

/** One entry in the picker. `id` is what `?dataset=` carries. */
export interface DatasetChoice {
	id: string;
	/** Total rows across the corpus's tables — the only size signal the listing carries. */
	rows: number;
	capabilities: string[];
	/** True for the entry the zone is currently rendering. */
	active: boolean;
	/** True when this is the backend's default DB, reached by having NO `?dataset=` at all. */
	isDefault: boolean;
}

/** The shape `listDatasets()` returns, declared structurally so this module imports no client.
 *
 *  `tables` and `capabilities` are OPTIONAL because the wire schema declares them so — a corpus that
 *  ships neither is a legitimate answer, and treating their absence as a type error would push a
 *  cast into the component where the missing case would then go unhandled. */
export interface DatasetSummaryLike {
	id: string;
	tables?: Record<string, { row_count: number }> | undefined;
	capabilities?: string[] | undefined;
}

/**
 * The picker's entries, in a stable order.
 *
 * `activeId` is what the descriptor actually loaded — NOT the query param. They differ in the normal
 * case: with no `?dataset=` the store derives the default corpus's id from the health endpoint, and
 * an entry that failed to mark itself active would leave the picker showing nothing selected on the
 * page everyone lands on first.
 */
export function datasetChoices(
	summaries: DatasetSummaryLike[],
	activeId: string | null,
	defaultId: string | null,
): DatasetChoice[] {
	return [...summaries]
		.sort((a, b) => a.id.localeCompare(b.id))
		.map((summary) => ({
			id: summary.id,
			rows: Object.values(summary.tables ?? {}).reduce(
				(total, table) => total + table.row_count,
				0,
			),
			capabilities: [...(summary.capabilities ?? [])].sort(),
			active: summary.id === activeId,
			isDefault: summary.id === defaultId,
		}));
}

/**
 * The URL that selects one dataset, preserving everything else on the current one.
 *
 * Two rules, both learned from what the page does with its query string:
 *
 * - The DEFAULT corpus drops the param entirely rather than naming itself. `?dataset=<default-id>`
 *   works, but it makes the plain URL and the explicit one two different strings for one place, and
 *   only the plain one is what the zone links to internally.
 * - Every OTHER param survives. The search page keeps its query, filters and mode in the URL, so
 *   rebuilding it from scratch would silently discard the search someone is in the middle of.
 */
export function datasetHref(current: URL, choice: { id: string; isDefault: boolean }): string {
	const next = new URL(current.href);
	if (choice.isDefault) next.searchParams.delete('dataset');
	else next.searchParams.set('dataset', choice.id);
	return `${next.pathname}${next.search}`;
}
