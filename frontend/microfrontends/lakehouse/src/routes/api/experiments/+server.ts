import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

// Embedded experiment tracking (#53) — the same-origin BFF for the deployed Perses "Model Training —
// experiment metrics" dashboard. It runs that dashboard's exact PromQL queries against GreptimeDB's
// Prometheus-compatible endpoint server-side and returns per-model series, so the /experiments page
// renders the real training metrics (OTLP → GreptimeDB, #18 — not MLflow) without ever exposing a
// credential to the browser: GreptimeDB is reached over its in-cluster ClusterIP, so the §5e edge
// auth (nginx auth_basic on the gateway's /greptime/ and /perses/ locations) is not in this path.
//
// Session-gated on a governed stack (mirroring the /capi catalog stance): model identity is FGA-gated
// everywhere else, so an anonymous front-door caller must not read the estate's model names + training
// activity straight off the metric store. Without OIDC configured (auth-off dev) it answers openly.
// Read-only and metrics-only — no user data, no write surface; when GREPTIME_API is unset it 501s.
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

// Per-model training CURVES (?model=<name>) for the registry's detail view — the same GreptimeDB
// metrics as time series via the Prometheus query_range endpoint (last 24 h, 5 min steps), so the
// detail page can plot honest per-run trajectories instead of only the latest scalar.
const CURVES = [
	{ key: 'rows', title: 'Rows seen per run', metric: 'lance_training_rows_seen' },
	{ key: 'features', title: 'Feature datasets per run', metric: 'lance_training_features' },
	{ key: 'runs', title: 'Cumulative training runs', metric: 'lance_training_runs_total' },
] as const;
const RANGE_S = 24 * 3600;
const STEP_S = 300;

type Point = { t: string; v: number };

/** Escape a label VALUE for a PromQL matcher (backslash, quote, newline). */
const escapeLabel = (v: string): string =>
	v.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n');

async function promqlRange(base: string, fetchFn: typeof fetch, query: string): Promise<Point[]> {
	const end = Math.floor(Date.now() / 1000);
	const q = new URLSearchParams({
		query,
		start: String(end - RANGE_S),
		end: String(end),
		step: String(STEP_S),
	});
	const res = await fetchFn(`${base}/v1/prometheus/api/v1/query_range?${q}`);
	if (!res.ok) throw new Error(`greptime ${res.status}`);
	const body = (await res.json()) as {
		data?: { result?: { values?: [number, string][] }[] };
	};
	// One series per query (aggregated by max); flatten its [unix, value] pairs.
	return (body.data?.result ?? [])
		.flatMap((r) => r.values ?? [])
		.map(([t, v]) => ({ t: new Date(t * 1000).toISOString(), v: Number(v) }))
		.filter((p) => Number.isFinite(p.v));
}

type Series = { model: string; value: number };

async function promql(base: string, fetchFn: typeof fetch, query: string): Promise<Series[]> {
	const url = `${base}/v1/prometheus/api/v1/query?query=${encodeURIComponent(query)}`;
	const res = await fetchFn(url);
	if (!res.ok) throw new Error(`greptime ${res.status}`);
	const body = (await res.json()) as {
		data?: { result?: { metric?: Record<string, string>; value?: [number, string] }[] };
	};
	return (body.data?.result ?? [])
		.map((r) => ({ model: r.metric?.lance_model ?? '?', value: Number(r.value?.[1] ?? '0') }))
		.filter((s) => Number.isFinite(s.value))
		.sort((a, b) => a.model.localeCompare(b.model));
}

export const GET: RequestHandler = async ({ url, fetch, locals }) => {
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to view experiment metrics' }, { status: 401 });
	}
	if (!GREPTIME_API) {
		return json(
			{ detail: 'experiment tracking requires the observability stack (GreptimeDB)' },
			{ status: 501 },
		);
	}
	// The per-model curve mode: `?model=<name>` → time series for the registry detail's plots. An
	// unknown model is not an error — its curves are honestly empty (no fabricated training history).
	const model = url.searchParams.get('model')?.trim();
	if (model) {
		try {
			const selector = `{lance_model="${escapeLabel(model)}"}`;
			const curves = await Promise.all(
				CURVES.map(async (c) => ({
					key: c.key,
					title: c.title,
					points: await promqlRange(GREPTIME_API, fetch, `max(${c.metric}${selector})`),
				})),
			);
			return json({ model, source: 'GreptimeDB (OTLP)', curves });
		} catch (err) {
			console.error(`experiments curves upstream failure: ${String(err)}`);
			return json({ detail: String(err) }, { status: 502 });
		}
	}
	try {
		const panels = await Promise.all(
			PANELS.map(async (p) => ({
				key: p.key,
				title: p.title,
				series: await promql(GREPTIME_API, fetch, p.query),
			})),
		);
		return json({
			dashboard: 'Model Training — experiment metrics',
			source: 'GreptimeDB (OTLP)',
			panels,
		});
	} catch (err) {
		console.error(`experiments proxy upstream failure: ${String(err)}`);
		return json({ detail: String(err) }, { status: 502 });
	}
};
