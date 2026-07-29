// Typed client for the CATALOG service via the /capi BFF proxy (the /api proxy covers lineage).
// Types are generated from docs/catalog-openapi.json (`bun run gen:types:catalog`) — never hand-mirrored.
// The describe route serializes with response_model_exclude_none, so its null fields arrive absent —
// read optional fields with `?? null` rather than trusting the generated required-nullable shape.
import type { components } from '@rask/api/generated/catalog';
import { requestJSON as request } from '$lib/http';

export type ModelSummary = components['schemas']['ModelSummary'];
export type ModelsList = components['schemas']['ModelsListResponse'];
export type ModelDescribe = components['schemas']['ModelDescribeResponse'];
/** One object under the model's artifact tree (path relative to the model root) — the describe
 * response's `artifacts` listing the registry detail renders as a table. */
export type ModelArtifact = components['schemas']['ModelArtifact'];
export type PromoteResponse = components['schemas']['PromoteResponse'];

const requestJSON = <T>(path: string, init?: RequestInit) => request<T>('/capi', path, init);

const enc = encodeURIComponent;

export const fetchModels = () => requestJSON<ModelsList>('v1/model');
export const fetchModel = (model: string) => requestJSON<ModelDescribe>(`v1/model/${enc(model)}`);

/** Bless `version` of `model` (candidate→blessed). Carries the signed-in user's session only — the BFF
 * refuses an anonymous promote outright (401) without forwarding anything. */
export const promoteModel = (model: string, version: number) =>
	requestJSON<PromoteResponse>(`v1/model/${enc(model)}/promote`, {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ version }),
	});
