// The stores-registry e2e fixture, shared by `attach-store.spec.ts`, `storage/browser.spec.ts` and the
// mock catalog that serves `/v1/stores`.
//
// This was `access-fixtures.ts` and carried the FGA workbench's tuples, DSL, model conditions and
// expand tree as well. Those left with the workbench itself (#105 → the home zone's `/settings/access`,
// where the fixture now lives beside its spec); what remained was the store registry, which was only
// ever here because the two shared one mock catalog.

/** The stores registry the mock catalog serves (`/v1/stores`, `/v1/stores/tiers`) — static, so the
 *  fullyParallel reads never race; attach WRITES are recorded per bearer like everything mutable. */
export const STORES = [
	{ name: 'wh', bucket: 'rask-wh', role: 'bronze', description: 'the warehouse', read_only: true },
	{
		name: 'ext',
		bucket: 'scans-1900',
		role: 'raw',
		description: 'external scans',
		read_only: true,
	},
];
