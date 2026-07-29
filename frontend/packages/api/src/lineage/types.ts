// @rask/api/lineage — the lineage-plane domain types, DERIVED from the backend OpenAPI contract; never
// hand-mirror the Pydantic models (that silently drifts). `../generated/lineage.ts` is generated from the
// committed docs/lineage-openapi.json (dumped by `make openapi`) — regenerate with `bun run gen:types` at
// the frontend root. Only domain constants live here by hand.
//
// One module for the whole estate: the admin, data and lineage zones each used to carry their own
// near-identical alias list over their own byte-identical copy of the generated file, so a schema rename
// had to be chased through three trees. This is the union of all three.
import type { components } from '../generated/lineage';

type S = components['schemas'];

export type DatasetGovernance = S['DatasetGovernance'];
export type GraphNode = S['GraphNode'];
export type GraphEdge = S['GraphEdge'];
export type LineageGraph = S['LineageGraph'];
export type EstateGraph = S['EstateGraph'];
export type ProducerInfo = S['ProducerInfo'];
export type Producers = S['Producers'];
export type ReaderInfo = S['ReaderInfo'];
export type Readers = S['Readers'];
export type Creator = S['Creator'];
export type DatasetSchema = S['DatasetSchema'];
export type SchemaField = S['SchemaField'];
export type EventRecord = S['EventRecord'];
export type SearchResults = S['SearchResults'];
export type Events = S['Events'];
export type DlqEvent = S['DlqEvent'];
export type DlqBacklog = S['DlqBacklog'];
export type DlqReplayResponse = S['DlqReplayResponse'];
export type DemoDataset = S['DemoDataset'];
export type DemoDatasets = S['DemoDatasets'];
export type Neighbors = S['Neighbors'];
export type DatasetRef = S['DatasetRef'];
export type RunStatus = S['RunStatus'];
export type Runs = S['Runs'];
export type RunInput = S['RunInput'];
export type RunInputs = S['RunInputs'];
export type ColumnRef = S['ColumnRef'];
export type ColumnEdge = S['ColumnEdge'];
export type ColumnGraph = S['ColumnGraph'];
export type ColumnNeighbors = S['ColumnNeighbors'];
export type DatasetSummary = S['DatasetSummary'];
export type Datasets = S['Datasets'];
export type JobSummary = S['JobSummary'];
export type Jobs = S['Jobs'];
export type Namespaces = S['Namespaces'];

// The medallion datasets, in flow order (raw -> bronze -> silver -> gold). Domain knowledge, not schema —
// used for icon/color parity on the known stage tables; graph layout depth is computed from the graph.
export const KNOWN = ['raw_events', 'bronze$events', 'silver$features', 'gold$catalog'] as const;
export const LAYER: Record<string, number> = {
	raw_events: 0,
	bronze$events: 1,
	silver$features: 2,
	gold$catalog: 3,
};
