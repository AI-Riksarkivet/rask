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
	// A table name is only meaningful inside the corpus that declares it, so switching corpora must
	// drop it. Carrying `?table=lines` onto a corpus with no `lines` produces a refusal from the
	// service the moment the page loads — a broken link built by our own picker.
	next.searchParams.delete('table');
	return `${next.pathname}${next.search}`;
}

/** One searchable table inside the active corpus. */
export interface TableChoice {
	name: string;
	/** The underlying Lance table it reads — shown so two similarly-named entries are tellable apart. */
	rowTable: string;
	active: boolean;
	/** True for the corpus's default, which is reached by having NO `?table=` at all. */
	isDefault: boolean;
}

/** The shape `DatasetView.searchTables` returns, declared structurally so this module imports no client. */
export interface SearchTableLike {
	name: string;
	row_table: string;
}

/**
 * The table picker's entries for one corpus.
 *
 * `activeName` is the `?table=` value — unlike the DATASET picker, which reads what the descriptor
 * loaded. The difference is real: a dataset is resolved server-side (the default corpus's id comes
 * from health), whereas the table is chosen purely by the URL, and with no param the default is
 * active by definition.
 */
export function tableChoices(tables: SearchTableLike[], activeName: string | null): TableChoice[] {
	return tables.map((table, index) => ({
		name: table.name,
		rowTable: table.row_table,
		isDefault: index === 0,
		active: activeName === null ? index === 0 : table.name === activeName,
	}));
}

/**
 * The URL that selects one searchable table, preserving everything else.
 *
 * Same two rules as `datasetHref`, for the same reasons: the DEFAULT drops the param rather than
 * naming itself, and every other param survives so a search in progress is not discarded.
 *
 * It additionally drops `table` whenever the DATASET changes, which `datasetHref` now does too — a
 * table name is only meaningful inside the corpus that declares it, so carrying `?table=lines` onto
 * a corpus with no `lines` would produce a refusal from the service on arrival.
 */
export function tableHref(current: URL, choice: { name: string; isDefault: boolean }): string {
	const next = new URL(current.href);
	if (choice.isDefault) next.searchParams.delete('table');
	else next.searchParams.set('table', choice.name);
	return `${next.pathname}${next.search}`;
}

/** The `?corpus=` values — the corpora searched ALONGSIDE the active one, fused by RRF. */
export function fanoutCorpora(url: URL): string[] {
	// De-duplicated, order preserved: the service does the same, and a picker that showed a corpus
	// selected twice would be describing a request the server never makes.
	return [...new Set(url.searchParams.getAll('corpus'))];
}

/**
 * The URL that adds or removes one corpus from the fan-out.
 *
 * A TOGGLE rather than a set-and-replace, because fanning out is cumulative by nature — you add a
 * second corpus to the one you are already reading, then a third. Replacing would make each click
 * discard the previous choice, which is the opposite of what the control is for.
 *
 * The ACTIVE corpus is never a fan-out entry: it is already being searched, and listing it would
 * double its rank contribution the moment the service de-duplicated one and not the other.
 */
export function toggleCorpusHref(current: URL, id: string, activeId: string | null): string {
	const next = new URL(current.href);
	const chosen = new Set(fanoutCorpora(current));
	if (chosen.has(id)) chosen.delete(id);
	else chosen.add(id);
	chosen.delete(activeId ?? '');

	next.searchParams.delete('corpus');
	for (const c of chosen) next.searchParams.append('corpus', c);
	return `${next.pathname}${next.search}`;
}
