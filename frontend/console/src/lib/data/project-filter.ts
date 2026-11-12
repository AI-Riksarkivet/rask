/**
 * The project column and the project filter of the catalog's lists (warehouses, namespaces, tables,
 * promotions).
 *
 * The filter is a VIEW, never a boundary. Every list reads estate-wide, and the catalog answers each
 * read with what the caller's grants allow; the filter only narrows, in this browser, rows the caller
 * was already given. It lives in the URL (`?project=<id>`), so a reload and a shared link show the same
 * rows, and nothing about the person is kept in a cookie or storage.
 *
 * A row's project is read from data the lists already hold: a warehouse names its project; a namespace
 * belongs to the warehouse its top namespace is bound to (the estate bindings); a table and a held
 * promotion belong to their namespace's. A namespace bound to no warehouse (the lakehouse's own
 * `provenance`, for one) has no project, and shows a dash.
 */
/** The URL parameter the filter is kept in. */
export const PROJECT_PARAM = 'project';

/** What these helpers read of a URL: its query. */
export type WithQuery = {
	readonly searchParams: { get(name: string): string | null; toString(): string };
};

/** The project the URL filters by, or `''` for every project. */
export function projectFilterOf(url: WithQuery): string {
	return url.searchParams.get(PROJECT_PARAM) ?? '';
}

/** The query of `url` with the filter set to `project` (`''` clears the parameter), without its
 *  `?`; `''` when nothing is left. */
export function queryWithProject(url: WithQuery, project: string): string {
	const params = new URLSearchParams(url.searchParams.toString());
	if (project) params.set(PROJECT_PARAM, project);
	else params.delete(PROJECT_PARAM);
	return params.toString();
}

/** One choice of the filter's select. */
export type ProjectOption = { value: string; label: string; disabled?: boolean };

/**
 * The filter's choices: every project, then the projects the caller can see (the catalog's
 * `GET /management/v1/projects`: every project to an estate observer, the caller's own to anyone
 * else) with the projects of the rows the list was answered (a grant on a table needs no
 * membership of its project). The id the filter names when it is neither stays on screen, so a
 * shared link still says what it filters by, as a choice that cannot be picked: it is no project the
 * caller can see.
 */
export function projectOptions(
	visible: readonly string[],
	ofRows: readonly string[],
	current: string,
): ProjectOption[] {
	const names = [...new Set([...visible, ...ofRows])].sort();
	const options: ProjectOption[] = [
		{ value: '', label: 'all projects' },
		...names.map((p) => ({ value: p, label: p })),
	];
	if (current && !names.includes(current))
		options.push({ value: current, label: `${current} (none you can see)`, disabled: true });
	return options;
}

/** Where the projects the caller can see stand. */
export type VisibleProjectsState =
	| { kind: 'loading' }
	| { kind: 'ready'; ids: ReadonlySet<string> }
	| { kind: 'failed'; reason: string };

/** What a filtered list shows: every row, the rows of the filter's project (none is an empty
 *  project), its loading state, why the filter cannot be applied, or that the filter names no
 *  project the caller can see. */
export type FilterVerdict =
	| { kind: 'all' }
	| { kind: 'apply' }
	| { kind: 'waiting' }
	| { kind: 'failed'; reason: string }
	| { kind: 'unseen' };

/**
 * What a list does with its filter. A project some answered row belongs to is applied whatever the
 * projects read says: the catalog gave the caller those rows, and a reader granted a table in a
 * project they are no member of must still see it there. Only a filter that matches no answered row
 * AND names no project the caller can see is unseen. `index` is the list's project index, or null for
 * a list whose rows name their project themselves.
 */
export function filterVerdict(req: {
	filter: string;
	index: ProjectIndexState | null;
	visible: VisibleProjectsState;
	rowProjects: ReadonlySet<string>;
}): FilterVerdict {
	const { filter, index, visible, rowProjects } = req;
	if (filter === '') return { kind: 'all' };
	if (index?.kind === 'loading') return { kind: 'waiting' };
	if (index?.kind === 'failed') return { kind: 'failed', reason: index.reason };
	if (rowProjects.has(filter)) return { kind: 'apply' };
	if (visible.kind === 'loading') return { kind: 'waiting' };
	if (visible.kind === 'failed') return { kind: 'failed', reason: visible.reason };
	return visible.ids.has(filter) ? { kind: 'apply' } : { kind: 'unseen' };
}

/** What a list needs to tell a row's project: each warehouse's project, and each bound namespace's
 *  warehouse. */
export type ProjectIndex = {
	warehouses: ReadonlyMap<string, string>;
	bindings: Readonly<Record<string, string>>;
};

export function projectIndex(
	warehouses: readonly { id: string; project: string }[],
	bindings: Readonly<Record<string, string>>,
): ProjectIndex {
	return { warehouses: new Map(warehouses.map((w) => [w.id, w.project])), bindings };
}

/**
 * The project a namespace belongs to: that of the warehouse its top bound namespace is bound to, read
 * from the outermost segment inwards (`acme$bronze` is bound through `acme`), or null when none is.
 */
export function projectOfNamespace(index: ProjectIndex, namespace: string): string | null {
	const segments = namespace.split('$');
	for (let depth = 1; depth <= segments.length; depth++) {
		const warehouse = index.bindings[segments.slice(0, depth).join('$')];
		if (warehouse !== undefined) return index.warehouses.get(warehouse) ?? null;
	}
	return null;
}

/** The project a table belongs to: its namespace's (every segment of its id but the last). */
export function projectOfTable(index: ProjectIndex, table: string): string | null {
	const cut = table.lastIndexOf('$');
	return cut < 0 ? null : projectOfNamespace(index, table.slice(0, cut));
}

/**
 * Where a list's project index stands. A filtered list shows rows only once it is `ready`: while it
 * is `loading` the list shows its loading state, and when it `failed` the list says the filter
 * cannot be applied and why, rather than showing every row as though it were filtered, or none as
 * though the project had none.
 */
export type ProjectIndexState =
	| { kind: 'loading' }
	| { kind: 'ready'; index: ProjectIndex }
	| { kind: 'failed'; reason: string };

/** Why a read the index needs failed, in words a reader can act on. */
export function readFailure(read: string, failure: { status: number; detail: string }): string {
	if (failure.status === 0) return `the ${read} read could not reach the catalog`;
	const detail = failure.detail ? `: ${failure.detail}` : '';
	return `the ${read} read answered HTTP ${failure.status}${detail}`;
}
