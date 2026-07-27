// Typed client for the MEDALLION (lance-ray) service via the /medallion BFF proxy. Distinct from the
// catalog (/capi) and lineage (/api) clients — lance-ray is its own backend (the cascade + train head).
import { requestJSON as request } from '$lib/http';

const requestMedallion = <T>(path: string, init?: RequestInit) =>
	request<T>('/medallion', path, init);

/** The /produce 202 body — a correlation `token` the cascade's run_ids derive from (or a 503 problem+json,
 * surfaced as ApiResult.status). */
export type ProduceResult = { status?: string; token?: string };
/** The /train 202 body — the correlation token + the model it will bless (or 409/422/503). */
export type TrainResult = { status?: string; token?: string; model?: string };

/** #64 fire the cascade head (raw→bronze→silver→gold). Admin-gated by lance-ray (can_administer on the
 * project) OR the service token; the BFF forwards only the signed-in user's bearer, so an anonymous
 * visitor is refused at the BFF (401) and a non-admin by lance-ray (403). */
export const triggerProduce = () => requestMedallion<ProduceResult>('produce', { method: 'POST' });

export type TrainFeature = { dataset: string; version?: number };

/** #64 request a training run — pointers only (claim-check): a `stage$name` feature dataset (optionally
 * version-pinned) and the target model. Same admin gate as produce. */
export const triggerTrain = (
	model: string,
	features: TrainFeature[],
	config: Record<string, unknown> = {},
) =>
	requestMedallion<TrainResult>('train', {
		method: 'POST',
		headers: { 'content-type': 'application/json' },
		body: JSON.stringify({ model, features, config }),
	});
