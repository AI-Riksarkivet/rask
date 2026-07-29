<script lang="ts">
	import { base } from '$app/paths';
	import { ingestIIIFVolume, type IngestAccepted } from '@rask/api';
	import { Card } from '@rask/ui/card';
	import { Button } from '@rask/ui/button';
	import { CloudDownload } from '@lucide/svelte';

	const ID_RE = /^[A-Za-z0-9._-]+$/;

	// The volume-ingest form — the P7a pipeline head. The retired upload/register door
	// (batches table) is gone: a volume is HARVESTED from the IIIF Image API by the medallion
	// producer (`POST /api/ingest-iiif`) into the raw page-image Lance dataset, and the cascade
	// (raw→bronze promotion, then the HTR movers) runs event-driven from its raw-write lineage
	// event. Pure action page — one mutation on submit, no reads, so no remote queries here.

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
			Harvests every page of the volume from the Riksarkivet IIIF Image API into the raw page-image
			dataset; the HTR cascade then runs event-driven. Progress lands in the pipeline-runs feed on the
			landing page.
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
			{busy ? 'Harvesting…' : 'Ingest volume'}
		</Button>

		{#if error}<p class="text-destructive text-sm">{error}</p>{/if}
		{#if result}
			<div class="space-y-1 rounded border border-emerald-600 p-3 text-sm">
				<p>
					Harvested <strong class="font-mono">{volumeId}</strong> — {result.pages} pages into
					<span class="font-mono">{result.dataset}</span> (cascade token
					<span class="font-mono">{result.token}</span>).
				</p>
				<a class="underline" href={base}>Back to the runs feed</a>
			</div>
		{/if}
	</Card>
</main>
