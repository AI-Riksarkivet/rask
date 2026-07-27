import { createLineageClient } from '@rask/api/lineage';
import { bff } from './http';

// This zone's binding of the shared lineage-plane client (@rask/api/lineage), which was three
// near-identical copies before. Spread into named module exports so call sites keep importing
// `$lib/api` unchanged.
export const {
	fetchEstateGraph,
	fetchProducers,
	fetchReaders,
	fetchEvents,
	fetchDlq,
	replayDlq,
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
	fetchGovernance,
	addDatasetTag,
	removeDatasetTag,
	setDatasetDescription,
	listDatasets,
	listJobs,
	listRuns,
} = createLineageClient(bff);
