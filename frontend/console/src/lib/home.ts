/**
 * What Home summarises, read off the doors every reader already calls: the tables the catalog lists for
 * the caller, counted per project, and the latest commits lineage's feed shows them. Both are the
 * caller's own view, governed by the doors, so two readers see two different Homes.
 */
import type { EventRecord } from '#lib/api/lineage/index.ts';

/** One project's tables; `project` is null for the tables in no project (the estate's own warehouse). */
export type ProjectTables = { project: string | null; total: number };

/** The caller's tables counted per project, projects by name and the projectless row last. */
export function tablesByProject(
	tables: readonly string[],
	projectOf: (id: string) => string | null,
): ProjectTables[] {
	const rows = new Map<string | null, ProjectTables>();
	for (const table of tables) {
		const project = projectOf(table);
		const row = rows.get(project) ?? { project, total: 0 };
		row.total += 1;
		rows.set(project, row);
	}
	return [...rows.values()].sort((a, b) =>
		a.project === null ? 1 : b.project === null ? -1 : a.project.localeCompare(b.project),
	);
}

/** One commit as Home lists it: when, what it did, the tables it wrote and who wrote them. */
export type Commit = {
	seq: number;
	at: string | null;
	operation: string;
	tables: string[];
	author: string | null;
};

/**
 * What a run did, from its OpenLineage job name `<producer>/<operation>.<table>` (the catalog's
 * `lance-catalog/insert.acme$bronze$orders`); a run named otherwise says its event type.
 */
function operationOf(event: Pick<EventRecord, 'job' | 'event_type'>): string {
	const match = /^[^/]+\/([^.]+)\./.exec(event.job ?? '');
	return match?.[1] ?? event.event_type ?? 'event';
}

/** The feed's events as commits, in the feed's order (newest first). */
export function recentCommits(
	events: readonly Pick<
		EventRecord,
		'seq' | 'event_type' | 'event_time' | 'job' | 'author' | 'outputs'
	>[],
): Commit[] {
	return events.map((event) => ({
		seq: event.seq,
		at: event.event_time ?? null,
		operation: operationOf(event),
		tables: event.outputs ?? [],
		author: event.author ?? null,
	}));
}
