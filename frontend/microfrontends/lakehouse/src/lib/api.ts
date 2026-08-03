import { createLineageClient } from '@rask/api/lineage';
import { bff } from './http';

// This zone's BROWSER binding of the shared lineage-plane client (@rask/api/lineage), which was three
// near-identical copies before. Spread into named module exports so call sites keep importing
// `$lib/api` unchanged.
//
// The DLQ pair and the governance quad have left: they now run on the zone server through
// `$lib/lineage/remote/lineage.remote.ts` (the transport ruling, area 2), which binds the SAME client to a
// server-side transport. What remains here rides the `/api/*` pass-through, whose deletion is blocked
// on the cross-zone workbench elements that consume it (the transport ruling caveat 2).
export const {
	fetchEstateGraph,
	fetchProducers,
	fetchReaders,
	fetchEvents,
	fetchRuns,
	fetchRunInputs,
	fetchUpstream,
	fetchDownstream,
	fetchSearch,
	fetchColumnGraph,
	fetchCreator,
	fetchSchema,
	fetchColumnUpstream,
	fetchColumnDownstream,
	listDatasets,
	listJobs,
	listRuns,
} = createLineageClient(bff);
