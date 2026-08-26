<script lang="ts">
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import {
		getIngestRunStatus,
		runLifecycle,
		type LifecycleResult,
	} from '$lib/remote/ingest.remote';
	import { bronzeDatasetId } from '$lib/remote/ingest-job';
	import { liveRead, lineageTick } from '$lib/live/tick.svelte';
	import { Button } from '@rask/ui/button';
	import { Card } from '@rask/ui/card';
	import {
		ArrowLeft,
		CircleAlert,
		CircleCheck,
		CircleX,
		Loader,
		Pause,
		Play,
		Square,
	} from '@lucide/svelte';

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

	// THE LINEAGE LINK MUST CARRY THE GRAPH'S ID, NOT THE USER'S WORDS. `run.dataset` is what a person
	// typed into the ETL form ("item3proof"); the graph stores what the write actually landed under
	// ("acme-bronze$item3proof"). Linking with the bare name resolved to a real route that reported
	// "No producing runs recorded for this dataset yet" — a link whose destination denied the run it
	// came from. Measured 2026-08-24: bare name 0 runs, qualified name 8.
	const lineageDatasetId = $derived(bronzeDatasetId(run?.project, run?.dataset));

	// PROGRESS. `units_total` was fetched and never rendered, so the page could say "4 done" and never
	// "4 of 500" — no progress bar was possible for exactly the long harvest where one matters. The
	// workflow publishes the enumerated total as CUSTOM STATUS specifically so this is answerable
	// while the run is still going; dropping it here wasted that.
	//
	// Zero means NOT YET KNOWN, not "no units": enumeration is an activity that runs after the run is
	// accepted, so there is a real window where the run exists and its total does not. Showing 0% then
	// would be a lie about a run that is working.
	const total = $derived(run?.units_total ?? 0);
	const totalKnown = $derived(total > 0);
	const percent = $derived(
		totalKnown ? Math.min(100, Math.round(((run?.units_done ?? 0) / total) * 100)) : 0,
	);

	// ── LIFECYCLE CONTROLS ─────────────────────────────────────────────────────────────────────────
	//
	// The doors existed before this page called them: `POST /ingests/{id}/{terminate,pause,resume}`
	// shipped with the Dapr audit drain and had ZERO callers in any zone, reachable only by curl. An
	// operator stopping a runaway harvest had to find a shell, a bearer and the run id — during an
	// incident, which is exactly when nobody wants to look those up.
	//
	// SHOWN DISABLED, NEVER HIDDEN (estate ruling). A finished run still renders all three buttons,
	// greyed, with the reason on the button itself. Hiding them would make "this run is COMPLETE" and
	// "this build has no terminate feature" look identical, and only one of those is worth knowing.
	let pending = $state<'' | 'terminate' | 'pause' | 'resume'>('');
	let outcome = $state<LifecycleResult | undefined>(undefined);

	// PAUSE and RESUME are mutually exclusive on state, TERMINATE applies to both. The server is the
	// authority — it answers 409 for a verb that does not apply — so these only decide what to grey
	// out. Guessing MORE than the server knows is how a UI starts refusing things the API allows.
	const live = $derived(run !== undefined && !TERMINAL.includes(run.status));
	const suspended = $derived(run?.status === 'SUSPENDED');
	const canTerminate = $derived(live);
	const canPause = $derived(live && !suspended);
	const canResume = $derived(suspended);

	function whyDisabled(verb: 'terminate' | 'pause' | 'resume'): string {
		if (run === undefined) return 'Run status is still loading.';
		if (!live && verb !== 'resume')
			return `This run is ${run.status} — there is nothing to ${verb}.`;
		if (verb === 'pause' && suspended) return 'This run is already paused.';
		if (verb === 'resume' && !suspended) return 'This run is not paused.';
		return '';
	}

	async function act(action: 'terminate' | 'pause' | 'resume') {
		pending = action;
		outcome = undefined;
		try {
			// The command single-flights the status refresh itself, so the page updates without a
			// second round trip here.
			outcome = await runLifecycle({ runId, action });
		} catch (err) {
			// A THROW IS NOT A REFUSAL. The command returns 409/503 as VALUES; reaching this branch
			// means the transport itself failed, and saying so is different from saying the door said no.
			outcome = { ok: false, status: 0, detail: err instanceof Error ? err.message : String(err) };
		} finally {
			pending = '';
		}
	}

	// TWO SIGNALS, because a run has two kinds of news and only one of them is on a cursor.
	//
	// 1. THE CURSOR — the estate's idiom (`liveRead` + `lineageTick`, the helper that replaced
	//    thirteen hand-rolled pollers in the lakehouse). An ingest emits lineage at START and again
	//    at COMPLETE/FAIL, so the cursor moves at both ENDS of a run: the page learns that a run
	//    finished the moment it finishes, instead of up to a full tick later. Keyed on `runId`, so
	//    navigating between runs re-reads at once rather than showing the previous run's cache.
	liveRead(
		lineageTick,
		() => {
			// `.catch(() => {})` is MANDATORY on any refresh a loop can repeat: one uncaught rejection
			// evicts the query from cache and silently kills further updates, leaving the page frozen
			// on a stale frame with no error shown.
			runQuery.refresh().catch(() => {});
		},
		() => runId,
	);

	// 2. POLL REASON: PROGRESS, and it is the one thing no cursor carries. `units_done` climbs once
	//    per fetched unit INSIDE the workflow and publishes nothing — the estate's cursors carry
	//    committed facts, and a harvest commits once, at the end. So a cursor-only page would show
	//    "0 units" for the entire run and then jump to the total, which is precisely the long harvest
	//    a progress bar exists for.
	//    BOUNDED, not ambient: the effect returns early once the run is settled, so the timer exists
	//    only while there is something to watch, and a finished run issues no requests at all. This
	//    is the single marked survivor on this page — the terminal transition above is the cursor's
	//    job, not this timer's.
	$effect(() => {
		if (settled) return;
		const timer = setInterval(() => {
			runQuery.refresh().catch(() => {});
		}, 2000);
		return () => clearInterval(timer);
	});
</script>

<svelte:head>
	<title>Ingest run — rask</title>
</svelte:head>

<main class="bg-background flex-1 overflow-auto">
	<!-- Centered column, same reason as the board: the old `m-4 max-w-3xl` card sat in the top-left
	     corner of a wide viewport. The header (back link + title + id) lives outside the card. -->
	<div class="mx-auto flex w-full max-w-3xl flex-col gap-4 p-6">
		<div class="space-y-1">
			<a
				class="text-muted-foreground hover:text-foreground inline-flex items-center gap-1 text-xs"
				href="{base}/ingest"
			>
				<ArrowLeft class="h-3 w-3" /> Ingest runs
			</a>
			<div class="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
				<div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
					<h1 class="text-xl font-semibold tracking-tight">Ingest run</h1>
					<span class="text-muted-foreground font-mono text-xs" data-testid="run-id">{runId}</span>
				</div>

				{#if !failed}
					<!-- All three ALWAYS rendered, disabled when they do not apply, with the reason in the
					     tooltip. See `whyDisabled` for why hiding them would be worse than greying them. -->
					<div class="flex items-center gap-2" data-testid="run-lifecycle">
						<Button
							variant="outline"
							size="sm"
							disabled={!canPause || pending !== ''}
							title={whyDisabled('pause') || 'Hold this run — it keeps its queue and consumer'}
							data-testid="run-pause"
							onclick={() => act('pause')}
						>
							<Pause class="h-3.5 w-3.5" />
							{pending === 'pause' ? 'Pausing…' : 'Pause'}
						</Button>
						<Button
							variant="outline"
							size="sm"
							disabled={!canResume || pending !== ''}
							title={whyDisabled('resume') || 'Resume from where this run was suspended'}
							data-testid="run-resume"
							onclick={() => act('resume')}
						>
							<Play class="h-3.5 w-3.5" />
							{pending === 'resume' ? 'Resuming…' : 'Resume'}
						</Button>
						<Button
							variant="destructive"
							size="sm"
							disabled={!canTerminate || pending !== ''}
							title={whyDisabled('terminate') || 'Stop scheduling further work on this run'}
							data-testid="run-terminate"
							onclick={() => act('terminate')}
						>
							<Square class="h-3.5 w-3.5" />
							{pending === 'terminate' ? 'Terminating…' : 'Terminate'}
						</Button>
					</div>
				{/if}
			</div>

			{#if outcome}
				<!-- THE DOOR'S OWN WORDS, carried through unchanged. Terminate's 202 says further
				     scheduling stops while an in-flight activity runs to completion; pause's says the run
				     still holds its queue and its consumer. Both were written to stop the misreading a
				     button invites, so softening either here would undo the reason they exist. -->
				<p
					class={outcome.ok
						? 'border-border text-muted-foreground rounded border p-3 text-xs'
						: 'border-destructive text-destructive rounded border p-3 text-xs'}
					data-testid="run-lifecycle-outcome"
				>
					{#if outcome.ok}
						<strong class="text-foreground"
							>Accepted{outcome.state ? ` — now ${outcome.state}` : ''}.</strong
						>
						{outcome.detail}
					{:else}
						<strong>Refused{outcome.status ? ` (${outcome.status})` : ''}.</strong> {outcome.detail}
					{/if}
				</p>
			{/if}
		</div>

		<Card class="space-y-5 p-6">
			{#if failed}
				<p
					class="border-destructive text-destructive flex items-center gap-2 rounded border p-3 text-sm"
					data-testid="run-unavailable"
				>
					<CircleX class="h-5 w-5 shrink-0" />
					<span>
						<strong>No such run.</strong> The ingest plane has no record of
						<span class="font-mono">{runId}</span>. Neither its accepted record nor a workflow for
						it exists — a run that had merely lost its progress would still answer here.
					</span>
				</p>
			{:else if run === undefined}
				<p class="text-muted-foreground flex items-center gap-2 text-sm">
					<Loader class="h-4 w-4 animate-spin" /> Loading run…
				</p>
			{:else}
				<div
					class="inline-flex items-center gap-2 rounded-full border px-3 py-1 {run.status ===
					'COMPLETE'
						? 'border-emerald-600/30'
						: run.status ===
							  'FAILED'
							? 'border-destructive/30'
							: run.status ===
								  'COMPLETE_WITH_ERRORS'
								? 'border-amber-600/30'
								: ''}"
					data-testid="run-status"
				>
					{#if run.status === 'COMPLETE'}
						<CircleCheck class="h-4 w-4 text-emerald-600" />
					{:else if run.status === 'FAILED'}
						<CircleX class="text-destructive h-4 w-4" />
					{:else if run.status === 'COMPLETE_WITH_ERRORS'}
						<CircleAlert class="h-4 w-4 text-amber-600" />
					{:else}
						<Loader class="text-muted-foreground h-4 w-4 animate-spin" />
					{/if}
					<span class="text-sm font-medium">{run.status}</span>
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

				<!-- PROGRESS, not just a counter. -->
				<div class="space-y-1" data-testid="run-progress">
					<div class="flex items-baseline justify-between text-sm">
						<span class="text-muted-foreground">Units</span>
						<span class="font-mono" data-testid="units-done">
							{run.units_done}{#if totalKnown}<span class="text-muted-foreground"> of {total}</span>{/if}
						</span>
					</div>
					{#if totalKnown}
						<div class="bg-muted h-2 w-full overflow-hidden rounded-full">
							<div
								class="h-full rounded-full bg-emerald-600 transition-[width] duration-500"
								style:width="{percent}%"
							></div>
						</div>
					{:else}
						<!-- Honest about the window between accept and enumeration: the run exists, its total
					     does not yet. A 0% bar here would misreport a working run as a stalled one. -->
						<p class="text-muted-foreground text-xs">
							Enumerating the source — the total is not known yet.
						</p>
					{/if}
				</div>

				<dl class="grid grid-cols-[auto_1fr] gap-x-8 gap-y-2 border-t pt-4 text-sm">
					<dt class="text-muted-foreground">Committed version</dt>
					<dd class="font-mono" data-testid="committed-version">
						{run.committed_version ?? '—'}
					</dd>

					<!-- The PUBLICATION half (§D2). A commit makes rows readable; only a publication makes
				     them ready, so a committed-but-unpublished run is a distinct state and not a
				     success. These fields were on the wire all along and stripped by the client schema. -->
					{#if run.published !== undefined && run.published !== null}
						<dt class="text-muted-foreground">Published</dt>
						<dd class="font-mono" data-testid="run-published">
							{run.published ? 'yes' : 'no'}
							{#if run.from_version != null && run.to_version != null}
								<span class="text-muted-foreground">(v{run.from_version} → v{run.to_version})</span>
							{/if}
						</dd>
					{/if}

					<!-- WHERE THE RUN'S PROVENANCE LIVES. Cross-zone (lineage is a lakehouse surface), so
					     `data-sveltekit-reload` is mandatory: without it SvelteKit soft-navigates into a
					     route this zone does not own and the link 404s. `@rask/zone-contract`'s
					     cross-zone-reload test is what enforces that, not a lint rule.

					     Rendered only when the wire names a dataset. A run recorded before the service
					     exposed it has none, and a guessed link is worse than no link. -->
					{#if lineageDatasetId}
						<dt class="text-muted-foreground">Lineage</dt>
						<dd class="font-mono">
							<a
								href={`/lakehouse/lineage/datasets/${encodeURIComponent(lineageDatasetId)}`}
								data-sveltekit-reload
								data-slot="run-lineage"
								class="hover:underline">{run.project ? `${run.project}/` : ''}{run.dataset}</a
							>
						</dd>
					{/if}
				</dl>

				{#if run.publish_error}
					<!-- A publish failure is deliberately NOT a failed run — the commit landed. But it must
				     be loud, or the rows sit readable-and-not-ready with nothing saying so. -->
					<p
						class="rounded border border-amber-600 p-3 text-sm text-amber-700"
						data-testid="publish-error"
					>
						<strong>Committed, but not published.</strong>
						{run.publish_error}
					</p>
				{:else if run.published === false && run.publish_reason}
					<p class="text-muted-foreground rounded border p-3 text-sm" data-testid="publish-reason">
						Not published — {run.publish_reason}
					</p>
				{/if}

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
	</div>
</main>
