import { command, query } from '$app/server';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import { catalogJSON, parsed } from '$lib/server/doors';
import type { ModelDescribe, ModelsList, PromoteResponse } from '../catalog';

// The model registry, in the zone's remote-function dialect — same names, same
// `ApiResult` shapes as the /capi client this replaces, transport only. The registry reads used to
// ride the generic GET-only /capi catch-all and the promote had a narrow route of its own; both now
// run on the zone server with the signed-in session's bearer, which keeps the promote's
// confused-deputy stance intact (a service credential never touches the catalog's can_promote rung)
// and moves the wire parse off the browser.

const enc = encodeURIComponent;

const ModelsListSchema = v.object({
	models: v.array(
		v.object({
			model: v.string(),
			latest_version: v.nullable(v.number()),
			blessed_version: v.nullable(v.number()),
		}),
	),
});

/** The describe response. `artifacts` is optional on the wire (the catalog serializes with
 *  response_model_exclude_none) and the metric maps are honestly nullable — "no blessed version yet"
 *  is a real answer the detail renders, never an empty object standing in for one. */
const ModelDescribeSchema = v.object({
	model: v.string(),
	latest_version: v.number(),
	blessed_version: v.nullable(v.number()),
	candidate_metrics: v.nullable(v.record(v.string(), v.unknown())),
	blessed_metrics: v.nullable(v.record(v.string(), v.unknown())),
	artifacts: v.optional(
		v.array(
			v.object({
				path: v.string(),
				size_bytes: v.number(),
				updated_at: v.nullable(v.string()),
			}),
		),
	),
});

const PromoteResponseSchema = v.object({
	model: v.string(),
	blessed_version: v.number(),
	tag: v.string(),
});

/** Every registered model with its candidate (latest) and blessed versions. */
export const fetchModels = query(async (): Promise<ApiResult<ModelsList>> =>
	parsed(await catalogJSON('/v1/model'), ModelsListSchema),
);

/** One model's detail: the candidate-vs-blessed metrics and its artifact listing. */
export const fetchModel = query(v.string(), async (model): Promise<ApiResult<ModelDescribe>> =>
	parsed(await catalogJSON(`/v1/model/${enc(model)}`), ModelDescribeSchema),
);

/** Bless `version` of `model` (candidate→blessed). Validator-gated by the catalog (can_promote)
 *  against the signed-in user; on success the registry list and THIS model's detail refresh in the
 *  same flight, so the row and the open drill-in cannot disagree about what is blessed. */
export const promoteModel = command(
	v.object({ model: v.string(), version: v.number() }),
	async ({ model, version }): Promise<ApiResult<PromoteResponse>> => {
		const result = parsed(
			await catalogJSON(`/v1/model/${enc(model)}/promote`, {
				method: 'POST',
				body: JSON.stringify({ version }),
			}),
			PromoteResponseSchema,
		);
		if (result.ok) {
			void fetchModels().refresh();
			void fetchModel(model).refresh();
		}
		return result;
	},
);
