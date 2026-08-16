// Medallion-tier derivation from NAMES (goal cond 3): the estate's namespaces follow the
// `[<project>-]raw|bronze|silver|gold[-media]` convention (#84 per-tenant zones; bare stage names
// are the projectless default path; `-media` is the multimodal sibling). This is a NAMING-derived
// hint — the honest wording everywhere is "derived", never presented as a catalog fact.

export type Stage = 'raw' | 'bronze' | 'silver' | 'gold';

export type StageInfo = {
	stage: Stage;
	/** The tenant prefix, or null on the projectless path (bare `raw`, `gold-media`, …). */
	project: string | null;
	/** True for the `-media` multimodal sibling zones. */
	media: boolean;
};

// Non-greedy project prefix + literal stage names, so `acme-data-silver` parses as
// project `acme-data`, stage `silver` (a greedy prefix would eat the stage).
/**
 * The identifier delimiter, in ONE place rather than the six literals that were scattered across this
 * zone (stage.ts twice, DangerZone, ColumnNode, ColumnLineage, the namespace detail page).
 *
 * It is a CONSTANT and not a fetched value, deliberately and with a known limit: the server's
 * `LANCE_NS_DELIMITER` is operator-settable, and the catalog exposes **no config endpoint at all**
 * (verified against docs/catalog-openapi.json — there is no `/v1/config` path), so the zone has no way
 * to learn the configured value. Making this truly dynamic therefore needs a backend endpoint first;
 * until then, centralising it means a deployment that changes the delimiter is one edit rather than a
 * hunt through six files that each looked local and correct.
 */
export const DELIMITER = '$';

const STAGE_RE = /^(?:([a-z0-9][a-z0-9_-]*?)-)?(raw|bronze|silver|gold)(-media)?$/;

/**
 * The medallion stage a namespace encodes, or null for a non-medallion namespace.
 *
 * Takes the LAST segment of a nested id. `STAGE_RE` is anchored and `$` is outside its character
 * class, so a nested id like `acme$acme-silver` matched nothing at all and every table under a nested
 * medallion zone rendered with no stage badge — while the identical namespace one rung higher showed
 * `silver`. The stage is a property of the zone the data sits IN, and for `acme$acme-silver` that zone
 * is `acme-silver`; the ancestors are tenancy, not tier.
 *
 * Ruled 2026-08-16 with the visible consequence stated: badges now appear where there were none.
 */
export function stageOf(namespace: string): StageInfo | null {
	const leaf = namespace.includes(DELIMITER)
		? namespace.slice(namespace.lastIndexOf(DELIMITER) + 1)
		: namespace;
	const m = STAGE_RE.exec(leaf);
	if (!m) return null;
	return { stage: m[2] as Stage, project: m[1] ?? null, media: m[3] !== undefined };
}

/**
 * The namespace of a `<ns>$…$<table>` id: EVERY segment but the last (a bare name is its own root —
 * the registry rule).
 *
 * `lastIndexOf`, not `indexOf`, and the difference is a bug rather than a nicety. Namespaces nest —
 * `namespace#parent: [warehouse, namespace]` in the FGA model, and the catalog's create door accepts
 * a nested id — so `acme$bronze$pages` is the table `pages` inside the namespace `acme$bronze`. Taking
 * the FIRST delimiter called that namespace `acme`, which is a different object.
 *
 * The backend is the authority and states it plainly: `parent_namespace_id` is "all segments but the
 * last" (`service_kit/governed/fga.py:187-201`), and that is the id the grant and check paths use. A
 * frontend deriving a different one disagrees with authz about which object a table belongs to.
 *
 * Four surfaces were wrong for a nested table: the Namespaces list folded it under its top-level
 * ancestor so the real namespace never appeared as a row at all; the warehouse detail page's namespace
 * list did the same; the Tables list linked its `namespace` column to an object one or more rungs too
 * high; and `stageOfTable` derived the medallion stage from the wrong name, so a table in
 * `acme$acme-silver` got no stage badge instead of `silver`.
 */
export function namespaceOfTable(table: string): string {
	return table.includes(DELIMITER) ? table.slice(0, table.lastIndexOf(DELIMITER)) : table;
}

/** The medallion stage of a table id, derived from its namespace segment. */
export function stageOfTable(table: string): StageInfo | null {
	return stageOf(namespaceOfTable(table));
}
