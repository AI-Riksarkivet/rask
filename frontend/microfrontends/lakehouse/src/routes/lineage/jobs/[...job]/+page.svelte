<script lang="ts">
	// Per-job detail (Marquez-parity): the job's latest runs (from the durable /runs board), the
	// datasets it reads (upstream) and writes (downstream), and the raw OpenLineage facets from
	// its most recent event. The route is a REST segment (`jobs/[...job]`) because a job identity
	// is `namespace/name` and so carries a `/` (e.g. `ray-jobs/embed_features`).
	import { ArrowLeft, Cpu } from '@lucide/svelte';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { enter } from '@rask/ui/motion';
	import { fetchEvents, fetchRuns } from '$lib/api';
	import type { EventRecord, RunStatus } from '@rask/api/lineage';
	import { lineageTick, liveRead } from '$lib/live/tick.svelte';

	const EVENTS_WINDOW = 200; // recent-events window the facet/neighbor fold reads
	const MAX_EVENTS_SHOWN = 25; // cap the rendered feed — the full payload is per-event on demand

	const jobId = $derived(page.params.job ?? '');

	// All three reads keyed by the job they were fetched FOR (latest-wins by derivation).
	let runState = $state<{ for: string; runs: RunStatus[] } | null>(null);
	let eventState = $state<{ for: string; events: EventRecord[] } | null>(null);
	// The ONE raw OpenLineage payload this page renders — the newest event of this job, fetched on its own
	// so the 200-event window can stay on the summary projection.
	let facetState = $state<{ for: string; event: Record<string, unknown> | null } | null>(null);

	const runs = $derived(runState?.for === jobId ? runState.runs : []);
	const events = $derived(eventState?.for === jobId ? eventState.events : []);

	// Upstream/downstream datasets folded from the job's events (inputs read / outputs written).
	const upstream = $derived([...new Set(events.flatMap((ev) => ev.inputs ?? []))].sort());
	const downstream = $derived([...new Set(events.flatMap((ev) => ev.outputs ?? []))].sort());

	// The latest event's facet payload — run + job facets exactly as emitted (spec-true, no reshaping).
	const latestFacets = $derived.by((): Record<string, unknown> | null => {
		const ev = facetState?.for === jobId ? facetState.event : null;
		if (!ev) return null;
		const run = (ev.run as { facets?: Record<string, unknown> } | undefined)?.facets;
		const job = (ev.job as { facets?: Record<string, unknown> } | undefined)?.facets;
		if (!run && !job) return null;
		return { ...(run ? { run: run } : {}), ...(job ? { job: job } : {}) };
	});

	async function load(current: string): Promise<void> {
		// `summary: true` is not optional here. Without it the wire carries every event's full OpenLineage
		// JSONB, measured live as alice through :8090: 471_217 bytes vs 46_625 with the flag — 10x, for a
		// page that folds only `job`, `inputs` and `outputs` out of the payload. The one thing that DOES
		// need the raw event is the facet panel, so it is fetched separately for the single newest event
		// of this job rather than for all 200.
		const [allRuns, feed] = await Promise.all([
			fetchRuns(),
			fetchEvents({ limit: EVENTS_WINDOW, summary: true }),
		]);
		if (jobId !== current) return; // latest-wins
		if (allRuns) {
			runState = { for: current, runs: (allRuns.runs ?? []).filter((r) => r.job === current) };
		}
		if (feed) {
			const mine = (feed.events ?? []).filter((ev) => ev.job === current);
			eventState = { for: current, events: mine };
			// `after` is an EXCLUSIVE keyset over `seq` (rows with seq < after), so `seq + 1` with limit 1
			// is exactly this one event — with its payload, since summary is off for this read alone.
			const newest = mine[0];
			const raw = newest ? await fetchEvents({ after: newest.seq + 1, limit: 1 }) : null;
			if (jobId !== current) return; // latest-wins, again: the second read can outlive a navigation
			facetState = { for: current, event: raw?.events?.[0]?.event ?? null };
		}
	}

	// Keyed live read: the job id re-reads on navigation, the lineage cursor re-reads when this job
	// actually emits. No timer.
	liveRead(
		lineageTick,
		(current: string) => load(current),
		() => jobId,
	);

	const stateColor = (s?: string | null) =>
		/FAIL|ABORT/i.test(s ?? '') ? 'var(--fail)' : s === 'COMPLETE' ? 'var(--ok)' : 'var(--amber)';
</script>

<svelte:head><title>{jobId} · lineage · lance</title></svelte:head>

<div class="page">
	<a class="back" href="{base}/lineage/jobs"><ArrowLeft size={12} /> jobs</a>
	<header>
		<h1 class="mono"><Cpu size={15} /> {jobId}</h1>
	</header>

	<div class="grid">
		<section {@attach enter({ y: 6 })}>
			<h2>Datasets</h2>
			<div class="rel-group">
				<span class="rel-label">Reads</span>
				{#each upstream as u (u)}
					<a class="rel-chip mono" href={`${base}/lineage/datasets/${encodeURIComponent(u)}`}>{u}</a
					>
				{:else}
					<span class="mut">no recorded inputs</span>
				{/each}
			</div>
			<div class="rel-group">
				<span class="rel-label">Writes</span>
				{#each downstream as d (d)}
					<a class="rel-chip mono" href={`${base}/lineage/datasets/${encodeURIComponent(d)}`}>{d}</a
					>
				{:else}
					<span class="mut">no recorded outputs</span>
				{/each}
			</div>

			<h2 class="later">Facets</h2>
			{#if latestFacets}
				<!-- The latest event's run/job facets exactly as emitted (OpenLineage spec-true). -->
				<pre class="mono json">{JSON.stringify(latestFacets, null, 2)}</pre>
			{:else}
				<p class="hint">No facets on this job's latest event.</p>
			{/if}
		</section>

		<section {@attach enter({ y: 6, delay: 0.05 })}>
			<h2>Latest runs <span class="count mono">{runs.length}</span></h2>
			{#if runs.length === 0}
				<p class="hint">No runs on the board for this job.</p>
			{/if}
			{#each runs as r (r.run_id)}
				<div class="run" class:fail={/FAIL|ABORT/i.test(r.state ?? '')}>
					<div class="run-top">
						<span class="badge" style:background={stateColor(r.state)}>
							{r.state}{#if r.progress_total}&nbsp;{r.progress_done ?? 0}/{r.progress_total}{/if}
						</span>
						<span class="mono runid">{r.run_id}</span>
						<span class="who">{r.author ?? '—'}</span>
					</div>
					<div class="who">{r.updated_at ?? ''} · {r.events} event{r.events === 1 ? '' : 's'}</div>
					{#if (r.outputs ?? []).length}
						<div class="outs">
							{#each r.outputs ?? [] as out (out)}
								<a
									class="rel-chip mono"
									href={`${base}/lineage/datasets/${encodeURIComponent(out)}`}>{out}</a
								>
							{/each}
						</div>
					{/if}
					{#if r.error_message}<div class="err">{r.error_message}</div>{/if}
				</div>
			{/each}

			<h2 class="later">Recent events <span class="count mono">{events.length}</span></h2>
			{#each events.slice(0, MAX_EVENTS_SHOWN) as ev (ev.seq)}
				<details class="event">
					<summary>
						<span class="badge" style:background={stateColor(ev.event_type)}>{ev.event_type}</span>
						<span class="who">{ev.author ?? '—'}</span>
						<span class="ts mono">{ev.event_time}</span>
					</summary>
					<pre class="mono json">{JSON.stringify(ev.event, null, 2)}</pre>
				</details>
			{:else}
				<p class="hint">No events in the recent window for this job.</p>
			{/each}
		</section>
	</div>
</div>

<style>
	.page {
		max-width: 980px;
		margin: 0 auto;
		padding: 40px 20px;
		width: 100%;
	}
	.back {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		color: var(--mut);
		font-size: 12px;
		text-decoration: none;
		margin-bottom: 8px;
	}
	.back:hover {
		color: var(--ink);
	}
	header {
		margin-bottom: 16px;
	}
	h1 {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 18px;
		margin: 0;
		word-break: break-all;
	}
	.grid {
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
		gap: 24px;
	}
	@media (max-width: 760px) {
		.grid {
			grid-template-columns: minmax(0, 1fr);
		}
	}
	h2 {
		font-size: 13px;
		margin: 0 0 10px;
		text-transform: uppercase;
		letter-spacing: 0.4px;
		color: var(--mut);
	}
	h2.later {
		margin-top: 18px;
	}
	.count {
		color: var(--faint);
		font-weight: 400;
	}
	.hint {
		color: var(--mut);
		font-size: 12px;
	}
	.mut {
		color: var(--faint);
		font-size: 11px;
	}
	.rel-group {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 5px;
		margin-bottom: 6px;
	}
	.rel-label {
		font-size: 10px;
		text-transform: uppercase;
		letter-spacing: 0.4px;
		color: var(--mut);
		margin-right: 2px;
	}
	.rel-chip {
		font-size: 11px;
		padding: 2px 8px;
		border-radius: 999px;
		border: 1px solid var(--line);
		background: var(--panel);
		color: var(--ink);
		text-decoration: none;
		transition:
			border-color 0.2s var(--ease),
			background 0.2s var(--ease);
	}
	.rel-chip:hover {
		border-color: var(--accent);
		background: color-mix(in srgb, var(--accent) 14%, transparent);
	}
	.badge {
		display: inline-block;
		padding: 1px 8px;
		border-radius: 999px;
		font-size: 11px;
		font-weight: 700;
		color: #06210f;
	}
	.run,
	.event {
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		padding: 8px 10px;
		margin-bottom: 8px;
		font-size: 12px;
		background: linear-gradient(180deg, var(--panel-2), var(--panel));
	}
	.run.fail {
		border-color: color-mix(in srgb, var(--fail) 55%, var(--line));
	}
	.run-top {
		display: flex;
		align-items: center;
		gap: 6px;
		flex-wrap: wrap;
	}
	.runid {
		font-size: 10.5px;
		color: var(--mut);
		word-break: break-all;
	}
	.run-top .who {
		margin-left: auto;
	}
	.who {
		color: var(--mut);
		font-size: 11px;
		margin-top: 4px;
	}
	.outs {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
		margin-top: 6px;
	}
	.err {
		color: var(--fail);
		margin-top: 4px;
	}
	.event summary {
		display: flex;
		gap: 8px;
		align-items: center;
		cursor: pointer;
	}
	.event .who {
		margin-top: 0;
	}
	.ts {
		color: var(--faint);
		font-size: 10.5px;
		margin-left: auto;
	}
	.json {
		font-size: 10.5px;
		background: #0c1018;
		border: 1px solid var(--line);
		border-radius: 6px;
		padding: 8px;
		margin: 6px 0 0;
		overflow: auto;
		max-height: 260px;
	}
</style>
