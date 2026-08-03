// The model registry's wire CONTRACTS. Types are generated from docs/catalog-openapi.json
// (`bun run gen:types:catalog`) — never hand-mirrored. The describe route serializes with
// response_model_exclude_none, so its null fields arrive absent — read optional fields with `?? null`
// rather than trusting the generated required-nullable shape.
//
// The registry TRANSPORT (list / describe / promote) lives in `remote/models.remote.ts` — the zone's
// remote-function dialect. A remote file may export only remote functions, so the types stay here.
import type { components } from '@rask/api/generated/catalog';

export type ModelSummary = components['schemas']['ModelSummary'];
export type ModelsList = components['schemas']['ModelsListResponse'];
export type ModelDescribe = components['schemas']['ModelDescribeResponse'];
/** One object under the model's artifact tree (path relative to the model root) — the describe
 * response's `artifacts` listing the registry detail renders as a table. */
export type ModelArtifact = components['schemas']['ModelArtifact'];
export type PromoteResponse = components['schemas']['PromoteResponse'];
