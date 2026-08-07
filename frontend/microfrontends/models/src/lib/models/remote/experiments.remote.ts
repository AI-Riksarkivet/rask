import { getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import * as v from 'valibot';
import type { ApiResult } from '@rask/api/client';

// Embedded experiment tracking (#53), in the zone's remote-function dialect — the /api/experiments
// route moved here VERBATIM (the transport ruling blocked-port #4): the deployed Perses "Model Training"
// dashboard's exact PromQL against GreptimeDB's Prometheus endpoint, run server-side so no credential
// or in-cluster URL ever reaches the browser. Session-gated on a governed stack exactly as before
// (model identity is FGA-gated everywhere else, so the metric store must not leak it to anonymous
// callers); auth-off dev answers openly; unset GREPTIME_API is the honest 501.

const GREPTIME_API = env.GREPTIME_API ?? '';

// The three panels of the deployed Perses `training` dashboard (chart/templates/perses-dashboards.yaml),
// query-for-query. Kept here as the single source so the page renders exactly what Perses shows.
const PANELS = [
	{
		key: 'runs',
		title: 'Training runs /s by model',
		query: 'sum by (lance_model) (rate(lance_training_runs_total[5m]))',
	},
	{
		key: 'rows',
		title: 'Rows seen (latest run) by model',
		query: 'max by (lance_model) (lance_training_rows_seen)',
	},
	{
		key: 'features',
		title: 'Feature datasets used (latest run) by model',
		query: 'max by (lance_model) (lance_training_features)',
	},
] as const;

// Per-model training CURVES for the registry's detail view — the same metrics as time series via the
// Prometheus query_range endpoint (last 24 h, 5 min steps).
const CURVES = [
	{ key: 'rows', title: 'Rows seen per run', metric: 'lance_training_rows_seen' },
	{ key: 'features', title: 'Feature datasets per run', metric: 'lance_training_features' },
	{ key: 'runs', title: 'Cumulative training runs', metric: 'lance_training_runs_total' },
] as const;
const RANGE_S = 24 * 3600;
const STEP_S = 300;

type Point = { t: string; v: number };
export type CurveSet = {
	model: string;
	source: string;
	curves: { key: string; title: string; points: Point[] }[];
};
export type Dashboard = {
	dashboard: string;
	source: string;
	panels: { key: string; title: string; series: { model: string; value: number }[] }[];
};

/** Escape a label VALUE for a PromQL matcher (backslash, quote, newline). */
const escapeLabel = (value: string): string =>
	value.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n');

function bearerHeaders(): Record<string, string> | undefined {
	// GreptimeDB in this path is the unauthenticated in-cluster endpoint; the bearer rides along only
	// so the hermetic mock can key its seeds per test (same rationale as the jetstream monitor fetch).
	const { locals } = getRequestEvent();
	return locals.session ? { authorization: `Bearer ${locals.session.accessToken}` } : undefined;
}

/** The two Prometheus-API envelopes, parsed (#94) — hand-mirrored wire shapes with no codegen, so
 *  a cast would let a drifted answer render as an empty chart instead of the offline state the
 *  caller already has for a throw. `looseObject` throughout: Prometheus answers carry more keys. */
const RangeEnvelopeSchema = v.looseObject({
	data: v.optional(
		v.looseObject({
			result: v.optional(
				v.array(
					v.looseObject({
						values: v.optional(v.array(v.tuple([v.number(), v.string()]))),
					}),
				),
			),
		}),
	),
});

const InstantEnvelopeSchema = v.looseObject({
	data: v.optional(
		v.looseObject({
			result: v.optional(
				v.array(
					v.looseObject({
						metric: v.optional(v.record(v.string(), v.string())),
						value: v.optional(v.tuple([v.number(), v.string()])),
					}),
				),
			),
		}),
	),
});

async function promqlRange(fetchFn: typeof fetch, promQuery: string): Promise<Point[]> {
	const end = Math.floor(Date.now() / 1000);
	const q = new URLSearchParams({
		query: promQuery,
		start: String(end - RANGE_S),
		end: String(end),
		step: String(STEP_S),
	});
	const res = await fetchFn(`${GREPTIME_API}/v1/prometheus/api/v1/query_range?${q}`, {
		headers: bearerHeaders(),
	});
	if (!res.ok) throw new Error(`greptime ${res.status}`);
	const body = v.parse(RangeEnvelopeSchema, await res.json());
	// One series per query (aggregated by max); flatten its [unix, value] pairs.
	return (body.data?.result ?? [])
		.flatMap((r) => r.values ?? [])
		.map(([t, val]) => ({ t: new Date(t * 1000).toISOString(), v: Number(val) }))
		.filter((p) => Number.isFinite(p.v));
}

async function promql(
	fetchFn: typeof fetch,
	promQuery: string,
): Promise<{ model: string; value: number }[]> {
	const url = `${GREPTIME_API}/v1/prometheus/api/v1/query?query=${encodeURIComponent(promQuery)}`;
	const res = await fetchFn(url, { headers: bearerHeaders() });
	if (!res.ok) throw new Error(`greptime ${res.status}`);
	const body = v.parse(InstantEnvelopeSchema, await res.json());
	return (body.data?.result ?? [])
		.map((r) => ({ model: r.metric?.lance_model ?? '?', value: Number(r.value?.[1] ?? '0') }))
		.filter((s) => Number.isFinite(s.value))
		.sort((a, b) => a.model.localeCompare(b.model));
}

/** The three-panel training dashboard (Perses parity). */
export const fetchExperimentsDashboard = query(async (): Promise<ApiResult<Dashboard>> => {
	const { fetch, locals } = getRequestEvent();
	if (locals.authEnabled && !locals.session) {
		return { ok: false, status: 401, detail: 'sign in to view experiment metrics' };
	}
	if (!GREPTIME_API) {
		return {
			ok: false,
			status: 501,
			detail: 'experiment tracking requires the observability stack (GreptimeDB)',
		};
	}
	try {
		const panels = await Promise.all(
			PANELS.map(async (p) => ({
				key: p.key,
				title: p.title,
				series: await promql(fetch, p.query),
			})),
		);
		return {
			ok: true,
			data: {
				dashboard: 'Model Training — experiment metrics',
				source: 'GreptimeDB (OTLP)',
				panels,
			},
		};
	} catch (err) {
		console.error(`experiments upstream failure: ${String(err)}`);
		return { ok: false, status: 502, detail: String(err) };
	}
});

/** Per-model training curves (?model= mode of the old route). An unknown model is not an error — its
 *  curves are honestly empty (no fabricated training history). */
export const fetchTrainingCurves = query(
	v.object({ model: v.string() }),
	async ({ model }): Promise<ApiResult<CurveSet>> => {
		const { fetch, locals } = getRequestEvent();
		if (locals.authEnabled && !locals.session) {
			return { ok: false, status: 401, detail: 'sign in to view experiment metrics' };
		}
		if (!GREPTIME_API) {
			return {
				ok: false,
				status: 501,
				detail: 'experiment tracking requires the observability stack (GreptimeDB)',
			};
		}
		try {
			const selector = `{lance_model="${escapeLabel(model)}"}`;
			const curves = await Promise.all(
				CURVES.map(async (c) => ({
					key: c.key,
					title: c.title,
					points: await promqlRange(fetch, `max(${c.metric}${selector})`),
				})),
			);
			return { ok: true, data: { model, source: 'GreptimeDB (OTLP)', curves } };
		} catch (err) {
			console.error(`experiments curves upstream failure: ${String(err)}`);
			return { ok: false, status: 502, detail: String(err) };
		}
	},
);
