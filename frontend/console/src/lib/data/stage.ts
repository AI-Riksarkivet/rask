// A namespace's TIER, as the catalog knows it (ADR 0160): the reserved namespace property `rask.tier`,
// declared once at namespace create by the project's admin, one of the three governed tiers. It is
// never read from a name: a namespace called `bronze` that declares nothing is untiered, and its name
// is the user's. A table's tier is the shallowest declaration on its namespace path (a deeper
// namespace may only repeat its ancestor's), and the path starts below the warehouse, which no door
// gives a tier. Tiers are opt-in: most namespaces declare none, and the console then shows none.

/**
 * The identifier grammar lives in `#lib/api/identifiers.ts`, the one copy every reader shares: a second
 * copy that split on the FIRST delimiter would name the wrong namespace for a nested one. Re-exported
 * here so a tier's callers import it from one place.
 */
import { DELIMITER } from '#lib/api/identifiers.ts';

export { parentNamespace as namespaceOfTable } from '#lib/api/identifiers.ts';

export type Stage = 'bronze' | 'silver' | 'gold';

export type StageInfo = { stage: Stage };

/** The reserved namespace property that declares a tier (`catalog.core.tier_policy.TIER_PROPERTY`). */
export const TIER_PROPERTY = 'rask.tier';

const TIERS: ReadonlySet<string> = new Set<Stage>(['bronze', 'silver', 'gold']);

function isStage(value: unknown): value is Stage {
	return typeof value === 'string' && TIERS.has(value);
}

/** The tier a namespace's own properties declare, or null when they declare none. */
export function declaredTier(properties: unknown): StageInfo | null {
	if (typeof properties !== 'object' || properties === null) return null;
	const tier: unknown = (properties as Record<string, unknown>)[TIER_PROPERTY];
	return isStage(tier) ? { stage: tier } : null;
}

/** The namespaces whose declaration can give `namespace` its tier, shallowest first: every prefix of
 *  its path below the warehouse, itself included. */
export function tierCandidates(namespace: string): string[] {
	const segments = namespace.split(DELIMITER);
	return segments.slice(1).map((_, i) => segments.slice(0, i + 2).join(DELIMITER));
}
