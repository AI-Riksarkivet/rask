<script lang="ts">
	import { base } from '$app/paths';
	import { ingestIIIFVolume, type IngestAccepted } from '@rask/api';
	import { Card } from '@rask/ui/card';
	import { Button } from '@rask/ui/button';
	import { CloudDownload } from '@lucide/svelte';

	const ID_RE = /^[A-Za-z0-9._-]+$/;

	// The volume-ingest form — the ingest plane's head (`POST /api/ingest`, open_ingest.md P1).
	// A volume is one SOURCE KIND among several: the door takes {kind, project, dataset, options}
	// and resolves the adapter from a registry, so adding S3-prefix ingest never touches this page.
	// The run is genuinely asynchronous — the form gets a run HANDLE back and the cascade proceeds
	// event-driven from the catalog's publication event. Pure action page: one mutation on submit,
	// no reads, so no remote queries here.

	let volumeId = $state('');
	let maxPages = $state('');
	let busy = $state(false);
	let error = $state<string | null>(null);
	let result = $state<IngestAccepted | null>(null);

	const validId = $derived(ID_RE.test(volumeId));
	const parsedMax = $derived(maxPages.trim() === '' ? null : Number(maxPages));
	const validMax = $derived(parsedMax === null || (Number.isInteger(parsedMax) && parsedMax >= 1));
	const canIngest = $derived(validId && validMax && !busy);

	async function ingest() {
		busy = true;
		error = null;
		result = null;
		try {
			result = await ingestIIIFVolume(volumeId, {
				...(parsedMax !== null ? { maxPages: parsedMax } : {}),
				// A retry after a 503 converges onto the same cascade run instead of double-firing it.
				idempotencyKey: `ui-${volumeId}`,
			});
		} catch (e: unknown) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			busy = false;
		}
	}
</script>

<svelte:head>
	<title>Ingest volume — RASK</title>
</svelte:head>

<main class="bg-background flex-1 overflow-auto">
	<Card class="m-4 max-w-2xl space-y-4 p-6">
		<h1 class="text-lg font-semibold">Ingest a IIIF volume</h1>
		<p class="text-muted-foreground text-sm">
			Accepts a run that harvests every page of the volume from the Riksarkivet IIIF Image API into the
			bronze page-image dataset; the HTR cascade then runs event-driven. The run starts in the
			background — this form returns as soon as it is dispatched, and progress lands in the
			pipeline-runs feed on the landing page.
		</p>

		<label class="block space-y-1">
			<span class="text-sm font-medium">Volume id</span>
			<input
				class="bg-background w-full rounded border px-3 py-2 font-mono"
				placeholder="e.g. A0068688"
				bind:value={volumeId}
				disabled={busy}
			/>
			{#if volumeId && !validId}
				<span class="text-destructive text-xs">Letters, digits, . - and _ only.</span>
			{/if}
		</label>

		<label class="block space-y-1">
			<span class="text-sm font-medium"
				>Max pages <span class="text-muted-foreground font-normal">(optional)</span></span
			>
			<input
				class="bg-background w-40 rounded border px-3 py-2"
				placeholder="all"
				inputmode="numeric"
				bind:value={maxPages}
				disabled={busy}
			/>
			{#if !validMax}
				<span class="text-destructive text-xs">A whole number ≥ 1, or leave empty.</span>
			{/if}
		</label>

		<Button onclick={ingest} disabled={!canIngest}>
			<CloudDownload class="h-4 w-4" />
			{busy ? 'Dispatching…' : 'Ingest volume'}
		</Button>

		{#if error}<p class="text-destructive text-sm">{error}</p>{/if}
		{#if result}
			<div class="space-y-1 rounded border border-emerald-600 p-3 text-sm">
				<!-- ACCEPTED, not "harvested". The old head declared 202 and then blocked through the
				     whole harvest, so this panel could honestly say the pages had landed. The run is now
				     genuinely asynchronous: nothing has been fetched yet when this renders, and claiming
				     otherwise would be the same declared-but-absent semantics the plane exists to fix. -->
				<p>
					Accepted <strong class="font-mono">{volumeId}</strong> — run
					<span class="font-mono">{result.run_id}</span>
					{#if result.deduplicated}
						<!-- Worth saying out loud: the user pressed the button and NO new work started.
						     Silence here reads as a no-op bug rather than idempotency working. -->
						<em>(already running — the same Idempotency-Key resolved to this run)</em>
					{/if}
				</p>
				<a class="underline" href={base}>Back to the runs feed</a>
			</div>
		{/if}
	</Card>
</main>
