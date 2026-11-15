/**
 * Seed routes that make namespaces DECLARE a tier, as the catalog stores one: the reserved namespace
 * property `rask.tier` on DescribeNamespace's answer (ADR 0160). The console reads a tier only from
 * there, never from a name, so a spec that means a badge declares it.
 */
export type Tier = 'bronze' | 'silver' | 'gold';

export function declaredTiers(tiers: Record<string, Tier>): Record<string, unknown> {
	return Object.fromEntries(
		Object.entries(tiers).map(([namespace, tier]) => [
			`POST /v1/namespace/${encodeURIComponent(namespace)}/describe`,
			{ properties: { 'rask.tier': tier } },
		]),
	);
}
