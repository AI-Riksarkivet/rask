<script lang="ts">
	import { base } from '$app/paths';
	import { listIngestRuns } from '$lib/remote/ingest.remote';
	import { liveRead, lineageTick } from '$lib/live/tick.svelte';
	import { Button } from '@rask/ui/button';
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
	<!-- Centered column, not a card hugging the top-left corner: on any wide viewport the old
	     `m-4 max-w-4xl` left everything crammed into one corner with a field of dead space beside
	     it. The header lives OUTSIDE the card — the card is the list, not the page. -->
	<div class="mx-auto flex w-full max-w-4xl flex-col gap-4 p-6">
		<div class="flex flex-wrap items-end justify-between gap-3">
			<div class="space-y-1">
				<h1 class="text-xl font-semibold tracking-tight">Ingest runs</h1>
				<p class="text-muted-foreground max-w-prose text-sm">
					Every run this plane has recorded, newest first — from the lineage graph, so the board survives
					a pod restart and is shared across replicas.
				</p>
			</div>
			<!-- The design system's Button (href form), not a hand-styled anchor: the base variant
			     already carries whitespace-nowrap + shrink-0, which is what stops "New run" wrapping
			     into a two-line pill when the header row tightens. -->
			<Button href="{base}/etl" size="sm">
				<Import data-icon="inline-start" /> New run
			</Button>
		</div>

		{#if loading}
			<Card class="p-6">
				<p class="text-muted-foreground flex items-center gap-2 text-sm">
					<Loader class="h-4 w-4 animate-spin" /> Reading the run board…
				</p>
			</Card>
		{:else if runs.length === 0}
			<!-- Honest about the two things an empty board can mean. A governed refusal and a genuinely
			     empty plane look identical from here, and claiming "no runs" for an unreadable board is
			     the same class of lie as reporting a failed read as loading. -->
			<Card class="p-6">
				<p class="text-muted-foreground text-sm">
					No ingest runs on the board. Either none have been started, or the lineage board is not
					readable with this session — start one from
					<a class="underline" href="{base}/etl">ETL</a> and it will appear here.
				</p>
			</Card>
		{:else}
			<Card class="overflow-hidden">
				<ul class="divide-y" data-testid="ingest-runs">
					{#each runs as run (run.run_id)}
						{@const pct = percent(run.progress_done, run.progress_total)}
						{@const terminal =
							run.state === 'COMPLETE' || run.state === 'FAIL' || run.state === 'FAILED'}
						<li>
							<!-- Linked ONLY with the producer's own id: `run.run_id` is the graph's derived
							     UUID5 and every link built from it was dead (measured — "No such run" on
							     each row). A row without `source_run_id` predates producers stating it and
							     renders unlinked WITH the reason — shown disabled, never hidden (ruling). -->
							<svelte:element
								this={run.source_run_id ? 'a' : 'div'}
								class="hover:bg-muted/50 flex items-center gap-3 px-4 py-3 {run.source_run_id
									? ''
									: 'opacity-60'}"
								href={run.source_run_id ? `${base}/ingest/${run.source_run_id}` : undefined}
								title={run.source_run_id
	? undefined
	: 'Recorded before run identity landed in the graph — no detail page can resolve it.'}
							>
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
									<!-- The TABLE is the headline — a bare UUID tells an operator nothing. -->
									{#if run.table}
										<span class="block truncate text-sm font-medium">{run.table}</span>
										<span class="text-muted-foreground block truncate font-mono text-[10px]"
											>{run.source_run_id ?? run.run_id}</span
										>
									{:else}
										<span class="block truncate font-mono text-xs">{run.source_run_id ?? run.run_id}</span>
									{/if}
									{#if run.error_message}
										<span class="text-destructive block truncate text-xs">{run.error_message}</span>
									{:else if run.updated_at}
										<span class="text-muted-foreground block text-xs">{run.updated_at}</span>
									{/if}
								</span>

								{#if pct !== null}
									<span class="flex shrink-0 items-center gap-2">
										<!-- A LIVE run gets a miniature of the detail page's bar — the board is where
										     an operator watches several harvests at once, and a bare "12 / 500" makes
										     them do the division. Terminal rows drop the bar: a full green strip on
										     every finished row is noise, the numbers are the record. -->
										{#if !terminal}
											<span class="bg-muted h-1.5 w-24 overflow-hidden rounded-full">
												<span
													class="block h-full rounded-full bg-emerald-600 transition-[width] duration-500"
													style:width="{pct}%"
												></span>
											</span>
										{/if}
										<span class="text-muted-foreground font-mono text-xs tabular-nums">
											{run.progress_done} / {run.progress_total}
										</span>
									</span>
								{/if}
								<span
									class="shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-medium tracking-wide uppercase {run.state === 'COMPLETE'
										? 'border-emerald-600/30 text-emerald-600'
										: run.state === 'FAIL' || run.state === 'FAILED'
											? 'border-destructive/30 text-destructive'
											: 'text-muted-foreground'}"
								>
									{run.state ?? '—'}
								</span>
							</svelte:element>
						</li>
					{/each}
				</ul>
			</Card>

			<!-- Said out loud, because a list that quietly stops at N reads as "that is all of them".
			     The server trims: /runs measured 330 KB for 875 runs on the live estate. -->
			{#if runs.length >= 50}
				<p class="text-muted-foreground text-xs">Showing the 50 most recent runs.</p>
			{/if}
		{/if}
	</div>
</main>
