<script lang="ts">
	import { base } from '$app/paths';
	import { listIngestRuns } from '$lib/remote/ingest.remote';
	import { liveRead, lineageTick } from '$lib/live/tick.svelte';
	import { Card } from '@rask/ui/card';
	import { CircleAlert, CircleCheck, CircleX, Import, Loader } from '@lucide/svelte';

	// The ingest RUN LIST — the page `/compute/ingest` did not have, which is why the sidebar's "Runs"
	// row pointed at a 404 (`nav-truth.test.ts` caught it and the row was removed until this existed).
	//
	// It reads LINEAGE, because the ingest service cannot list its own runs: three routes, and a
	// `RunStore` with only get/put whose production implementation is a per-pod dict that is
	// deliberately not durable. The durable record of which runs exist is the graph every run writes
	// to at START and again at COMPLETE/FAIL. See `listIngestRuns` for the trade that buys.

	const runsQuery = listIngestRuns();

	// The estate's cursor idiom, not a timer. An ingest emits lineage at BOTH ends of a run, so the
	// board refreshes when a run starts and when it finishes — which is exactly what a list needs.
	// (The per-run page keeps one marked timer for its progress COUNTER, which no cursor carries.)
	liveRead(lineageTick, () => {
		runsQuery.refresh().catch(() => {});
	});

	const runs = $derived(runsQuery.current ?? []);
	const loading = $derived(runsQuery.current === undefined && !runsQuery.error);

	function percent(done: number | null, total: number | null): number | null {
		if (!total || total <= 0 || done === null) return null;
		return Math.min(100, Math.round((done / total) * 100));
	}
</script>

<svelte:head>
	<title>Ingest runs — RASK</title>
</svelte:head>

<main class="bg-background flex-1 overflow-auto">
	<Card class="m-4 max-w-4xl space-y-4 p-6">
		<div class="flex items-center justify-between gap-2">
			<div>
				<h1 class="text-lg font-semibold">Ingest runs</h1>
				<p class="text-muted-foreground text-sm">
					Every run this plane has recorded, newest first — from the lineage graph, so the board
					survives a pod restart and is shared across replicas.
				</p>
			</div>
			<a
				class="bg-primary text-primary-foreground inline-flex items-center gap-2 rounded px-3 py-1.5 text-sm"
				href="{base}/etl"
			>
				<Import class="h-4 w-4" /> New run
			</a>
		</div>

		{#if loading}
			<p class="text-muted-foreground flex items-center gap-2 text-sm">
				<Loader class="h-4 w-4 animate-spin" /> Reading the run board…
			</p>
		{:else if runs.length === 0}
			<!-- Honest about the two things an empty board can mean. A governed refusal and a genuinely
			     empty plane look identical from here, and claiming "no runs" for an unreadable board is
			     the same class of lie as reporting a failed read as loading. -->
			<p class="text-muted-foreground text-sm">
				No ingest runs on the board. Either none have been started, or the lineage board is not
				readable with this session — start one from
				<a class="underline" href="{base}/etl">ETL</a> and it will appear here.
			</p>
		{:else}
			<ul class="divide-y rounded border" data-testid="ingest-runs">
				{#each runs as run (run.run_id)}
					{@const pct = percent(run.progress_done, run.progress_total)}
					<li>
						<a class="hover:bg-muted/50 flex items-center gap-3 p-3" href="{base}/ingest/{run.run_id}">
							{#if run.state === 'COMPLETE'}
								<CircleCheck class="h-4 w-4 shrink-0 text-emerald-600" />
							{:else if run.state === 'FAIL' || run.state === 'FAILED'}
								<CircleX class="text-destructive h-4 w-4 shrink-0" />
							{:else if run.error_message}
								<CircleAlert class="h-4 w-4 shrink-0 text-amber-600" />
							{:else}
								<Loader class="text-muted-foreground h-4 w-4 shrink-0 animate-spin" />
							{/if}

							<span class="min-w-0 flex-1">
								<span class="block truncate font-mono text-xs">{run.run_id}</span>
								{#if run.error_message}
									<span class="text-destructive block truncate text-xs">{run.error_message}</span>
								{:else if run.updated_at}
									<span class="text-muted-foreground block text-xs">{run.updated_at}</span>
								{/if}
							</span>

							{#if pct !== null}
								<span class="text-muted-foreground shrink-0 font-mono text-xs">
									{run.progress_done} / {run.progress_total}
								</span>
							{/if}
							<span class="shrink-0 text-xs font-medium">{run.state ?? '—'}</span>
						</a>
					</li>
				{/each}
			</ul>

			<!-- Said out loud, because a list that quietly stops at N reads as "that is all of them".
			     The server trims: /runs measured 330 KB for 875 runs on the live estate. -->
			{#if runs.length >= 50}
				<p class="text-muted-foreground text-xs">Showing the 50 most recent runs.</p>
			{/if}
		{/if}
	</Card>
</main>
