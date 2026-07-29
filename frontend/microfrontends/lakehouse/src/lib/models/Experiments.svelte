<script lang="ts">
	// `/experiments` — the embedded "Model Training — experiment metrics" dashboard (#53): the deployed
	// Perses training dashboard's panels, rendered from the real GreptimeDB metrics (OTLP → GreptimeDB,
	// #18 — MLflow is not used). Data comes through the /api/experiments BFF, which runs the dashboard's
	// exact PromQL queries server-side against GreptimeDB's Prometheus endpoint — no credential ever
	// reaches the browser. Governed without a session → 401 (sign-in state); no observability → 501
	// (unavailable state); auth-off dev → open data. Each renders its honest state below.
	import { BarChart3, ExternalLink, RefreshCw, ShieldAlert } from '@lucide/svelte';
	import { page } from '$app/state';
	import { requestJSON } from '$lib/http';

	// POLL REASON: a decaying rate has no event. This is the one surviving timer in this zone, and it
	// survives on purpose. Every other panel re-reads on a
	// cursor because its value changes when the estate changes. This panel does not: it renders
	// `rate(lance_training_runs_total[5m])` and friends, a Prometheus rate over a MOVING five-minute
	// window, whose value falls as the clock advances even when nothing whatsoever happens. Driving it
	// from the lineage cursor would freeze a decaying rate at its last non-zero value and call it live —
	// a worse lie than a poll. There is no event that means "the window moved", so the clock stays.
	const POLL_MS = 5000;

	// Return here after the OIDC round-trip (the shell's ?redirect= contract, nav-user.svelte).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	type Panel = { key: string; title: string; series: { model: string; value: number }[] };
	type Dashboard = { dashboard: string; source: string; panels: Panel[] };

	let data = $state<Dashboard | null>(null);
	let lastStatus = $state(0);
	let settled = $state(false);
	let inflight = 0; // latest-wins: only the newest poll may publish

	const unauthorized = $derived(data === null && settled && lastStatus === 401);
	const unavailable = $derived(data === null && settled && lastStatus === 501);
	// 0 stays IN the offline set: after the first settle, a fetch-level failure (network down, BFF timeout)
	// reports status 0 and must render as offline, not hang on the loading message (audit finding).
	const offline = $derived(data === null && settled && ![200, 401, 501].includes(lastStatus));

	async function load(): Promise<void> {
		const seq = ++inflight;
		const res = await requestJSON<Dashboard>('/api', 'experiments');
		if (seq !== inflight) return; // a newer poll superseded this one
		settled = true;
		if (res.ok) {
			data = res.data;
			lastStatus = 200;
		} else {
			lastStatus = res.status;
		}
	}

	$effect(() => {
		load();
		const timer = setInterval(load, POLL_MS);
		return () => clearInterval(timer);
	});

	function fmt(v: number): string {
		return Number.isInteger(v) ? String(v) : v.toFixed(3);
	}
	// A per-panel max so the bars scale within their own panel.
	function scale(series: { value: number }[]): number {
		return Math.max(1, ...series.map((s) => s.value));
	}
</script>

<div class="page">
	<header>
		<BarChart3 size={16} />
		<h1>Experiments</h1>
		<span class="sub mono">model training metrics · OTLP → GreptimeDB (not MLflow)</span>
	</header>

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>
				This stack is governed — <a href={loginHref} data-sveltekit-reload>sign in</a> to view experiment
				metrics.
			</p>
		</div>
	{:else if unavailable}
		<div class="empty">
			<p>
				Experiment tracking needs the observability stack (GreptimeDB) — not enabled on this stack.
			</p>
		</div>
	{:else if offline}
		<div class="empty">
			<RefreshCw size={16} />
			<p>Metrics store unreachable (HTTP {lastStatus}) — retrying.</p>
		</div>
	{:else if data === null}
		<div class="empty"><p>Loading…</p></div>
	{:else}
		<p class="src mono">{data.dashboard} · source: {data.source}</p>
		{#each data.panels as panel (panel.key)}
			<section>
				<h2>{panel.title}</h2>
				{#if panel.series.length === 0}
					<p class="mut">No training runs recorded yet — a `POST /train` produces the first.</p>
				{:else}
					{@const max = scale(panel.series)}
					<ul class="bars">
						{#each panel.series as s (s.model)}
							<li>
								<span class="model mono">{s.model}</span>
								<span class="bar"><span class="fill" style={`width:${(s.value / max) * 100}%`}></span></span
								>
								<span class="val mono">{fmt(s.value)}</span>
							</li>
						{/each}
					</ul>
				{/if}
			</section>
		{/each}
		<p class="foot mono">
			<ExternalLink size={11} /> full dashboard in Perses at
			<span class="path">/perses/</span> (edge-authenticated)
		</p>
	{/if}
</div>

<style>
	.page {
		max-width: 860px;
		margin: 0 auto;
		padding: 56px 20px 40px;
	}
	header {
		display: flex;
		align-items: baseline;
		gap: 10px;
		margin-bottom: 10px;
	}
	h1 {
		font-size: 20px;
		margin: 0;
	}
	h2 {
		font-size: 13px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--faint);
		margin: 0 0 8px;
	}
	.sub,
	.src {
		color: var(--faint);
		font-size: 12px;
	}
	.src {
		margin: 0 0 20px;
	}
	section {
		margin-bottom: 22px;
	}
	.bars {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 5px;
	}
	.bars li {
		display: grid;
		grid-template-columns: 180px 1fr 64px;
		align-items: center;
		gap: 10px;
		font-size: 12px;
	}
	.model {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--ink);
	}
	.bar {
		height: 12px;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		overflow: hidden;
	}
	.fill {
		display: block;
		height: 100%;
		background: color-mix(in srgb, var(--ok) 60%, var(--panel-2));
	}
	.val {
		text-align: right;
		color: var(--mut);
	}
	.mut {
		color: var(--faint);
		font-size: 12px;
	}
	.foot {
		color: var(--faint);
		font-size: 11px;
		display: flex;
		align-items: center;
		gap: 5px;
		margin-top: 24px;
	}
	.path {
		color: var(--mut);
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--mut);
		padding: 32px 0;
	}
</style>
