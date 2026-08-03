/**
 * The foreign-panel catalogue: which element script serves each panel, from ITS OWN zone's origin.
 * A module (not page state) because TWO consumers need it: the compositor page seeds panels with
 * these as `params`, and `ForeignPanel` falls back to it when params are ABSENT — which is exactly
 * what happens when a panel is added through the "+" picker or restored from a layout saved before
 * the entry carried params. Keyed by REGISTRY COMPONENT KEY; panel ids derive from it
 * (`lakehouse-runs`, `lakehouse-runs-2`, … — see uniquePanelId), so stripping the `-N` suffix
 * recovers the key from any live panel id.
 */
export type ForeignSource = { src: string; tag: string };

/** One cache-buster per PAGE LOAD. The element bundles ship under FIXED names (the catalogue must
 *  hold stable URLs), which lets a long-lived browser session keep serving a cached bundle across
 *  zone deploys — the user sees last hour's panels while a fresh session sees today's, which reads
 *  as "still broken" (observed repeatedly, live). One token per load = one fetch per zone per
 *  visit, and a plain refresh is ALWAYS current. */
const BUST = Date.now();

export const FOREIGN: Record<string, ForeignSource> = {
	'compute-jobs': { src: '/compute/elements/compute-elements.js', tag: 'rask-compute-jobs' },
	'compute-cluster': { src: '/compute/elements/compute-elements.js', tag: 'rask-compute-cluster' },
	'compute-actors': { src: '/compute/elements/compute-elements.js', tag: 'rask-compute-actors' },
	'compute-serve': { src: '/compute/elements/compute-elements.js', tag: 'rask-compute-serve' },
	'lakehouse-runs': {
		src: '/lakehouse/elements/lakehouse-elements.js',
		tag: 'rask-lakehouse-runs',
	},
	'lakehouse-events': {
		src: '/lakehouse/elements/lakehouse-elements.js',
		tag: 'rask-lakehouse-events',
	},
	'lakehouse-graph': {
		src: '/lakehouse/elements/lakehouse-elements.js',
		tag: 'rask-lakehouse-graph',
	},
	'lakehouse-datasets': {
		src: '/lakehouse/elements/lakehouse-elements.js',
		tag: 'rask-lakehouse-datasets',
	},
	'lakehouse-audit': {
		src: '/lakehouse/elements/lakehouse-elements.js',
		tag: 'rask-lakehouse-audit',
	},
	'compute-overview': {
		src: '/compute/elements/compute-elements.js',
		tag: 'rask-compute-overview',
	},
	'compute-logs': {
		src: '/compute/elements/compute-elements.js',
		tag: 'rask-compute-logs',
	},
	'lakehouse-access': {
		src: '/lakehouse/elements/lakehouse-elements.js',
		tag: 'rask-lakehouse-access',
	},
	'lakehouse-ops': {
		src: '/lakehouse/elements/lakehouse-elements.js',
		tag: 'rask-lakehouse-ops',
	},
	'lakehouse-columns': {
		src: '/lakehouse/elements/lakehouse-elements.js',
		tag: 'rask-lakehouse-columns',
	},
};

/** Resolve a live panel's source by its REGISTRY COMPONENT KEY — `api.component`, which dockview
 *  preserves through picker-adds, duplicates (`runs-2-2` still has component `lakehouse-runs`) and
 *  save/restore, killing the id-suffix failure class the review confirmed. The LIVE catalogue wins
 *  over any persisted params: a layout saved before an element script moved must follow the move,
 *  not brick (persisted-params-shadow-the-catalogue, also confirmed). Params survive only as a
 *  fallback for a component key the catalogue no longer names. */
export function resolveForeign(
	componentKey: string,
	params: Partial<ForeignSource>,
): ForeignSource | null {
	const live = FOREIGN[componentKey];
	const found = live ?? (params.src && params.tag ? { src: params.src, tag: params.tag } : null);
	if (found === null) return null;
	return { src: `${found.src}?v=${BUST}`, tag: found.tag };
}
