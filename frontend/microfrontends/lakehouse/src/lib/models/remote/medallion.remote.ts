import { command, getRequestEvent } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';
import type { ProduceResult, TrainResult } from '../medallion';

// The medallion (lance-ray) trigger door, in the zone's remote-function dialect — the last blocked
// port of open_transport.md area 2, unblocked by the mock-observability harness. Same contract as the
// deleted /medallion/[action] route: ONLY the signed-in user's bearer is forwarded (the service token
// never leaves the web pod, so lance-ray's admin gate is enforced against a real user), an anonymous
// visitor on an OIDC tier is refused locally with 401, and an unreachable medallion is 502 — the
// status the route answered, which PipelineControl renders as "unreachable" rather than a fake denial.
// The action allowlist dissolves into function identity (produce/train are the only functions).

const MEDALLION_API = env.MEDALLION_API ?? 'http://localhost:8000';

async function medallionPOST(
	action: 'produce' | 'train',
	body: unknown,
	idem?: string,
): Promise<ApiResult<unknown>> {
	const { fetch, locals } = getRequestEvent();
	if (locals.authEnabled && !locals.session) {
		return { ok: false, status: 401, detail: 'sign in to trigger the pipeline' };
	}
	const headers: Record<string, string> = { 'content-type': 'application/json' };
	if (locals.session) headers['authorization'] = `Bearer ${locals.session.accessToken}`;
	// The trigger's 503+Retry-After idempotency contract only pairs a retry with its first attempt if
	// the key rides along — carried as an argument now that there is no request header to forward.
	if (idem) headers['idempotency-key'] = idem;
	try {
		const res = await fetch(`${MEDALLION_API}/${action}`, {
			method: 'POST',
			headers,
			body: body === undefined ? undefined : JSON.stringify(body),
		});
		const data: unknown = await res.json().catch(() => ({}));
		if (!res.ok) {
			const detail =
				data && typeof data === 'object' && 'detail' in data
					? String((data as { detail: unknown }).detail)
					: `medallion answered ${res.status}`;
			return { ok: false, status: res.status, detail };
		}
		return { ok: true, data };
	} catch (err) {
		return { ok: false, status: 502, detail: String(err) };
	}
}

/** #64 fire the cascade head (raw→bronze→silver→gold). Admin-gated by lance-ray. */
export const triggerProduce = command(
	v.optional(v.object({ idem: v.optional(v.string()) })),
	async (arg): Promise<ApiResult<ProduceResult>> =>
		(await medallionPOST('produce', undefined, arg?.idem)) as ApiResult<ProduceResult>,
);

/** #64 request a training run — pointers only (claim-check): a `stage$name` feature dataset
 *  (optionally version-pinned) and the target model. Same admin gate as produce. */
export const triggerTrain = command(
	v.object({
		model: v.string(),
		features: v.array(v.object({ dataset: v.string(), version: v.optional(v.number()) })),
		config: v.optional(v.record(v.string(), v.unknown()), {}),
		idem: v.optional(v.string()),
	}),
	async ({ model, features, config, idem }): Promise<ApiResult<TrainResult>> =>
		(await medallionPOST('train', { model, features, config }, idem)) as ApiResult<TrainResult>,
);
