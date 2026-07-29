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
const STAGE_RE = /^(?:([a-z0-9][a-z0-9_-]*?)-)?(raw|bronze|silver|gold)(-media)?$/;

/** The medallion stage a namespace name encodes, or null for a non-medallion namespace. */
export function stageOf(namespace: string): StageInfo | null {
	const m = STAGE_RE.exec(namespace);
	if (!m) return null;
	return { stage: m[2] as Stage, project: m[1] ?? null, media: m[3] !== undefined };
}

/** The namespace segment of a `<ns>$<table>` id (a bare name is its own root — the registry rule). */
export function namespaceOfTable(table: string): string {
	return table.includes('$') ? table.slice(0, table.indexOf('$')) : table;
}

/** The medallion stage of a table id, derived from its namespace segment. */
export function stageOfTable(table: string): StageInfo | null {
	return stageOf(namespaceOfTable(table));
}
