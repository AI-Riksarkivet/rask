<script lang="ts">
	// Per-model training curves for the registry detail (goal cond 9) — LayerChart line plots over
	// the real GreptimeDB time series (OTLP → GreptimeDB, #18; the /api/experiments BFF's ?model=
	// curve mode runs the query_range server-side). Curves render only where data exists; a model
	// with no recorded series gets the honest empty state, never a fabricated flat line.
	import { LineChart } from 'layerchart';
	import { scaleTime } from 'd3-scale';
	import { RefreshCw, ShieldAlert } from '@lucide/svelte';
	import { requestJSON } from '$lib/http';

	let { model }: { model: string } = $props();

	type Point = { t: string; v: number };
	type Curve = { key: string; title: string; points: Point[] };
	type Curves = { model: string; source: string; curves: Curve[] };

	let data = $state<Curves | null>(null);
	let lastStatus = $state(0);
	let settled = $state(false);
	let inflight = 0;

	const unauthorized = $derived(data === null && settled && lastStatus === 401);
	const unavailable = $derived(data === null && settled && lastStatus === 501);
	const offline = $derived(data === null && settled && ![200, 401, 501].includes(lastStatus));

	async function load(): Promise<void> {
		const current = model;
		const seq = ++inflight;
		const res = await requestJSON<Curves>(
			'/api',
			`experiments?model=${encodeURIComponent(current)}`,
		);
		if (seq !== inflight || model !== current) return; // latest-wins
		settled = true;
		if (res.ok) {
			data = res.data;
			lastStatus = 200;
		} else {
			data = null;
			lastStatus = res.status;
		}
	}

	$effect(() => {
		void model;
		load();
	});

	const withData = $derived((data?.curves ?? []).filter((c) => c.points.length > 0));

	type ChartPoint = { t: Date; v: number };
	function chartPoints(c: Curve): ChartPoint[] {
		return c.points.map((p) => ({ t: new Date(p.t), v: p.v }));
	}
</script>

<div class="curves">
	<h4>Training curves <span class="src mono">GreptimeDB (OTLP) · last 24 h</span></h4>
	{#if unauthorized}
		<p class="mut"><ShieldAlert size={13} /> Sign in to view training curves.</p>
	{:else if unavailable}
		<p class="mut">Training curves need the observability stack (GreptimeDB) — not enabled here.</p>
	{:else if offline}
		<p class="mut"><RefreshCw size={13} /> Metrics store unreachable (HTTP {lastStatus}).</p>
	{:else if data === null}
		<p class="mut">Loading curves…</p>
	{:else if withData.length === 0}
		<p class="mut">
			No training series recorded for this model in the last 24 h — a `POST /train` run writes the
			first points.
		</p>
	{:else}
		<div class="grid">
			{#each withData as curve (curve.key)}
				<figure class="plot" aria-label="Curve {curve.title}">
					<figcaption>{curve.title}</figcaption>
					<div class="canvas">
						<LineChart
							data={chartPoints(curve)}
							x={(d: ChartPoint) => d.t}
							xScale={scaleTime()}
							y={(d: ChartPoint) => d.v}
							yNice
							padding={{ left: 36, bottom: 20, top: 6, right: 8 }}
							series={[{ key: 'value', color: 'var(--accent, #6aa9ff)' }]}
						/>
					</div>
				</figure>
			{/each}
		</div>
	{/if}
</div>

<style>
	.curves h4 {
		display: flex;
		align-items: baseline;
		gap: 8px;
		font-size: 12px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--faint);
		margin: 0 0 8px;
	}
	.src {
		text-transform: none;
		letter-spacing: 0;
		font-size: 11px;
	}
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
		gap: 14px;
	}
	.plot {
		margin: 0;
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		background: var(--panel-2);
		padding: 10px 12px;
	}
	.plot figcaption {
		font-size: 11px;
		color: var(--mut);
		margin-bottom: 6px;
	}
	.canvas {
		height: 150px;
	}
	.mut {
		display: flex;
		align-items: center;
		gap: 6px;
		color: var(--faint);
		font-size: 12px;
		margin: 0;
	}
	.mono {
		font-family: ui-monospace, monospace;
	}
</style>
