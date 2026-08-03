<script lang="ts">
	import { page } from '$app/state';
	import { getIngestRunStatus } from '$lib/remote/ingest.remote';
	import { Card } from '@rask/ui/card';
	import { CircleAlert, CircleCheck, CircleX, Loader } from '@lucide/svelte';

	// One ingest run's status (open_ingest.md A20). The run is genuinely asynchronous, so this page
	// is the only honest place to learn what happened to it — the POST returns a handle, not a result.
	//
	// Everything shown here comes from the ENGINE, not from a cache written at accept time. The
	// service reads the workflow's own durable history, so a run survives the pod that started it and
	// this page keeps answering across a restart.

	const runId = $derived(page.params.run_id ?? '');
	const runQuery = $derived(getIngestRunStatus(runId));
	const run = $derived(runQuery.current);

	// A FAILED READ IS NOT A LOADING STATE, and conflating them is the defect this page shipped with
	// for one revision: an unknown run id left `current` undefined forever, so the page rendered
	// "Loading run…" permanently at HTTP 200 while polling a run that would never exist. To an
	// operator that is indistinguishable from a run still working — the single worst thing a status
	// page can get wrong, because it turns a typo into an apparently-live harvest.
	const failed = $derived(runQuery.error);

	// Terminal states stop the poll, and so does a failure. Polling a finished run forever is how a
	// status page becomes the busiest client of the service it reports on; polling a run that does
	// not exist is that plus a permanent lie on screen.
	const TERMINAL = ['COMPLETE', 'COMPLETE_WITH_ERRORS', 'FAILED'];
	const settled = $derived(
		failed !== undefined || (run !== undefined && TERMINAL.includes(run.status)),
	);
	const errorEntries = $derived(Object.entries(run?.errors ?? {}));

	$effect(() => {
		if (settled) return;
		const timer = setInterval(() => {
			// `.catch(() => {})` is MANDATORY on a polled refresh: one uncaught rejection evicts the
			// query from cache and silently kills the loop, so a transient blip would leave the page
			// frozen on a stale frame with no error and no further updates.
			runQuery.refresh().catch(() => {});
		}, 2000);
		return () => clearInterval(timer);
	});
</script>

<svelte:head>
	<title>Ingest run — RASK</title>
</svelte:head>

<main class="bg-background flex-1 overflow-auto">
	<Card class="m-4 max-w-3xl space-y-4 p-6">
		<div class="flex items-center gap-2">
			<h1 class="text-lg font-semibold">Ingest run</h1>
			<span class="text-muted-foreground font-mono text-xs" data-testid="run-id">{runId}</span>
		</div>

		{#if failed}
			<p
				class="border-destructive text-destructive flex items-center gap-2 rounded border p-3 text-sm"
				data-testid="run-unavailable"
			>
				<CircleX class="h-5 w-5 shrink-0" />
				<span>
					<strong>No such run.</strong> The ingest plane has no record of
					<span class="font-mono">{runId}</span>. Neither its accepted record nor a workflow for it
					exists — a run that had merely lost its progress would still answer here.
				</span>
			</p>
		{:else if run === undefined}
			<p class="text-muted-foreground flex items-center gap-2 text-sm">
				<Loader class="h-4 w-4 animate-spin" /> Loading run…
			</p>
		{:else}
			<div class="flex items-center gap-2" data-testid="run-status">
				{#if run.status === 'COMPLETE'}
					<CircleCheck class="h-5 w-5 text-emerald-600" />
				{:else if run.status === 'FAILED'}
					<CircleX class="text-destructive h-5 w-5" />
				{:else if run.status === 'COMPLETE_WITH_ERRORS'}
					<CircleAlert class="h-5 w-5 text-amber-600" />
				{:else}
					<Loader class="text-muted-foreground h-5 w-5 animate-spin" />
				{/if}
				<span class="font-medium">{run.status}</span>
			</div>

			<!-- A8. Shown ABOVE the numbers on purpose: a run that landed data with no provenance
			     record still reports rows and a version, so an operator reading top-down would
			     otherwise see success first and the hole second, if at all. -->
			{#if run.defect}
				<p
					class="border-destructive text-destructive rounded border p-3 text-sm"
					data-testid="run-defect"
				>
					<strong>Provenance defect.</strong>
					{run.defect}
				</p>
			{/if}

			<dl class="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
				<dt class="text-muted-foreground">Units done</dt>
				<dd class="font-mono" data-testid="units-done">{run.units_done}</dd>

				<dt class="text-muted-foreground">Committed version</dt>
				<dd class="font-mono" data-testid="committed-version">
					{run.committed_version ?? '—'}
				</dd>
			</dl>

			{#if errorEntries.length > 0}
				<!-- Named, not counted. "3 units failed" tells an operator a number; the unit keys tell
				     them which pages to look at, which is the only form of the answer that is actionable. -->
				<section class="space-y-1" data-testid="run-errors">
					<h2 class="text-sm font-medium">Units that refused to land ({errorEntries.length})</h2>
					<ul class="space-y-1 text-xs">
						{#each errorEntries as [unit, reason] (unit)}
							<li class="rounded border p-2">
								<span class="font-mono">{unit}</span>
								<span class="text-muted-foreground"> — {reason}</span>
							</li>
						{/each}
					</ul>
				</section>
			{/if}
		{/if}
	</Card>
</main>
