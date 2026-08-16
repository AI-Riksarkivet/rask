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
 * The identifier grammar now lives in `@rask/api/identifiers`, not here.
 *
 * It was centralised into this module first, which was only half a fix: `stage.ts` is the LAKEHOUSE's
 * `$lib`, and zones do not share `$lib`, so the `home` zone could not reach it and kept its own copy —
 * a copy that split on the FIRST delimiter and therefore named the wrong namespace. Re-exported here so
 * this module's existing callers and tests keep their import site.
 */
import { leafSegment, parentNamespace } from '@rask/api/identifiers';

export { parentNamespace as namespaceOfTable } from '@rask/api/identifiers';

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
	const m = STAGE_RE.exec(leafSegment(namespace));
	if (!m) return null;
	return { stage: m[2] as Stage, project: m[1] ?? null, media: m[3] !== undefined };
}

/** The medallion stage of a table id, derived from its namespace segment. */
export function stageOfTable(table: string): StageInfo | null {
	return stageOf(parentNamespace(table));
}
