// @rask/api/lineage — the lineage-plane domain types, DERIVED from the backend OpenAPI contract; never
// hand-mirror the Pydantic models (that silently drifts). `../generated/lineage.ts` is generated from the
// committed docs/lineage-openapi.json (dumped by `make openapi`) — regenerate with `bun run gen:types` at
// the frontend root.
import type { components } from '../generated/lineage';

type S = components['schemas'];

export type EventRecord = S['EventRecord'];
export type Events = S['Events'];
export type DlqEvent = S['DlqEvent'];
export type DlqBacklog = S['DlqBacklog'];
export type DlqReplayResponse = S['DlqReplayResponse'];
