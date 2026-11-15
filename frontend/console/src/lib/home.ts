/**
 * What Home summarises, read off the doors every reader already calls: the tables the catalog lists for
 * the caller, grouped by project and tier, and the latest commits lineage's feed shows them. Both are the
 * caller's own view, governed by the doors, so two readers see two different Homes.
 */
import type { EventRecord } from '#lib/api/lineage/index.ts';
import type { Stage, StageInfo } from '#lib/data/stage.ts';

/** One project's tables per declared tier; `other` is a table whose namespaces declare none. `project` is
 *  null for the tables in no project (the estate's own warehouse). */
export type TierRow = {
	project: string | null;
	bronze: number;
	silver: number;
	gold: number;
	other: number;
	total: number;
};

/** The three tiers a namespace may declare, in the order a reader expects them. */
export const TIER_ORDER = ['bronze', 'silver', 'gold'] as const satisfies readonly Stage[];

/** The caller's tables counted per project and declared tier, projects by name and the projectless
 *  row last. `tierOf` answers the tier a table's namespace declares (ADR 0160), never one from a name. */
export function tablesByProjectAndTier(
	tables: readonly string[],
	projectOf: (id: string) => string | null,
	tierOf: (id: string) => StageInfo | null,
): TierRow[] {
	const rows = new Map<string | null, TierRow>();
	for (const table of tables) {
		const project = projectOf(table);
		const row = rows.get(project) ?? { project, bronze: 0, silver: 0, gold: 0, other: 0, total: 0 };
		row[tierOf(table)?.stage ?? 'other'] += 1;
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
