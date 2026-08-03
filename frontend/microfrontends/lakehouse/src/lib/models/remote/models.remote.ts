import { command, getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import { parse } from '@rask/api';
import type { ApiResult } from '@rask/api/client';
import type { ModelDescribe, ModelsList, PromoteResponse } from '../catalog';

// The model registry, in the zone's remote-function dialect (open_transport.md) — same names, same
// `ApiResult` shapes as the /capi client this replaces, transport only. The registry reads used to
// ride the generic GET-only /capi catch-all and the promote had a narrow route of its own; both now
// run on the zone server with the signed-in session's bearer, which keeps the promote's
// confused-deputy stance intact (a service credential never touches the catalog's can_promote rung)
// and moves the wire parse off the browser.

const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

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

function bearerHeaders(): Record<string, string> {
	const { locals } = getRequestEvent();
	const bearer = locals.session?.accessToken;
	return bearer ? { authorization: `Bearer ${bearer}` } : {};
}

async function catalogJSON(path: string, init?: RequestInit): Promise<ApiResult<unknown>> {
	const { fetch } = getRequestEvent();
	try {
		const res = await fetch(`${CATALOG_API}${path}`, {
			...init,
			headers: {
				...bearerHeaders(),
				...(init?.body ? { 'content-type': 'application/json' } : {}),
			},
		});
		if (!res.ok) {
			let detail = `catalog answered ${res.status}`;
			try {
				const body: unknown = await res.json();
				if (body && typeof body === 'object' && 'detail' in body) detail = String(body.detail);
			} catch {
				/* a non-JSON error body keeps the status-line detail */
			}
			return { ok: false, status: res.status, detail };
		}
		return { ok: true, data: await res.json() };
	} catch (err) {
		// The routes this replaces answered an unreachable catalog with 502 rather than throwing, and
		// the registry reads that status to render its offline state instead of a boundary error.
		return { ok: false, status: 502, detail: String(err) };
	}
}

function parsed<T>(result: ApiResult<unknown>, schema: v.GenericSchema<unknown, T>): ApiResult<T> {
	if (!result.ok) return result;
	try {
		return { ok: true, data: parse(schema, result.data) };
	} catch (err) {
		return { ok: false, status: 502, detail: `catalog contract drift: ${String(err)}` };
	}
}

/** Every registered model with its candidate (latest) and blessed versions. */
export const fetchModels = query(
	async (): Promise<ApiResult<ModelsList>> =>
		parsed(await catalogJSON('/v1/model'), ModelsListSchema),
);

/** One model's detail: the candidate-vs-blessed metrics and its artifact listing. */
export const fetchModel = query(
	v.string(),
	async (model): Promise<ApiResult<ModelDescribe>> =>
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
