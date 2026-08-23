<script lang="ts">
	/**
	 * Standalone Atlas route shell — a thin wrapper around <AtlasMap> for the
	 * `/atlas` nav entry. The bidirectional cross-filter, EVōC clusters, Text/
	 * Visual toggle, color-by, and hover popover all live in <AtlasMap> (shared
	 * with the search page's Map view via the cross-filter runes store); this
	 * shell owns the route layout: a vertical resizable split of [map | player]
	 * over the current map-selection results table.
	 *
	 * Lance-native throughout — no DuckDB / Mosaic / parquet. Points come from
	 * GET /api/atlas/points, selection detail from POST /api/atlas/chunks (by
	 * stable _rowid), single playback from GET /api/atlas/chunk (all in <AtlasMap>).
	 */
	import AtlasMap from './AtlasMap.svelte';
	import { ResizableSplit } from '@rask/ui/resizable-split';
	import PlayerPane from '$lib/components/player-pane.svelte';
	import HitTable, { TABLE_COLUMNS } from '$lib/components/hit-table.svelte';
	import { activeView, type Hit } from '@rask/explorer-api';
	import { Columns3, Check, Tags } from '@lucide/svelte';
	import { Button } from '@rask/ui/button';
	import SendToProjectDialog from '$lib/components/SendToProjectDialog.svelte';
	import { selectionKeys, sendRefusal } from './atlas-send';

	let active = $state<Hit | null>(null);
	let tableHits = $state<Hit[]>([]);
	let selectionTotal = $state(0);

	// Columns the user can show. `caption` (the frame's Swedish caption) is joined
	// from chunk_frames by the /chunks endpoint — it isn't a column on chunks.
	const OFFERED_COLS = TABLE_COLUMNS();
	// Default column set is descriptor-driven: the thumbnail, the declared
	// metadata fields (by name), then time/body/caption. Keys not backed by a
	// TABLE_COLUMN are ignored by the render filter, so this is safe as-is.
	const DEFAULT_COLS = [
		'thumbnail',
		...activeView().metadataFields.map((m) => m.field),
		'start',
		'text',
		'caption',
	];
	const COLS_KEY = 'lance-media-atlas-cols-v2'; // bumped so the caption column shows by default

	let visibleCols = $state<string[]>([...DEFAULT_COLS]);
	let showColsPicker = $state(false);

	// Hydrate the saved column set once on the client (avoids an SSR mismatch).
	$effect(() => {
		try {
			const v = localStorage.getItem(COLS_KEY);
			if (!v) return;
			const parsed: unknown = JSON.parse(v);
			if (!Array.isArray(parsed)) return;
			const keys = new Set(OFFERED_COLS.map((c) => c.key));
			const valid = parsed.filter((k): k is string => typeof k === 'string' && keys.has(k));
			if (valid.length) visibleCols = valid;
		} catch {}
	});

	function setCols(next: string[]): void {
		visibleCols = next;
		try {
			localStorage.setItem(COLS_KEY, JSON.stringify(next));
		} catch {}
	}

	function toggleCol(key: string): void {
		setCols(
			visibleCols.includes(key) ? visibleCols.filter((k) => k !== key) : [...visibleCols, key],
		);
	}

	function onSelectionHits(hits: Hit[], total: number): void {
		tableHits = hits;
		selectionTotal = total;
	}

	// ── LABEL THE REGION ──────────────────────────────────────────────────────────────────────
	// The atlas could always lasso and the selection dead-ended in this table. A region of an
	// embedding projection is usually a SEMANTIC cluster, so one lasso is very often one label —
	// which makes this the cheapest high-volume labelling gesture in the product, and it produced
	// nothing. `SendToProjectDialog` has declared `origin: 'atlas'` all along; nothing passed it.
	let sendOpen = $state(false);

	/** The keys the selection would send — built with the DESCRIPTOR's own `keyPath`, so they are
	 *  byte-identical to every other key in the estate (percent-encoded per part). */
	const sendKeys = $derived(selectionKeys(tableHits, (hit) => activeView().keyPath(hit)));

	/** Why this selection cannot be sent, or null. The load-bearing case is a lasso bigger than the
	 *  fetch: hits are capped while `selectionTotal` is the TRUE size, so sending the fetched subset
	 *  would queue a fraction of the region and report success. */
	const refusal = $derived(sendRefusal({ fetched: tableHits.length, total: selectionTotal }));
</script>

<div class="h-full min-h-0">
	<ResizableSplit
		orientation="vertical"
		storageKey="lance-media-atlas-vsplit"
		minLeft={260}
		minRight={140}
		initial={0.66}
	>
		{#snippet left()}
			<!-- top: map | player -->
			<ResizableSplit
				storageKey="lance-media-atlas-split"
				minLeft={420}
				minRight={320}
				initial={0.66}
			>
				{#snippet left()}
					<AtlasMap bind:active {onSelectionHits} />
				{/snippet}
				{#snippet right()}
					<div class="bg-muted/30 h-full min-h-0">
						<PlayerPane hit={active} />
					</div>
				{/snippet}
			</ResizableSplit>
		{/snippet}

		{#snippet right()}
			<!-- bottom: results table for the current map selection -->
			<div class="border-border bg-card/30 flex h-full min-h-0 flex-col border-t">
				<div class="border-border flex items-center gap-2 border-b px-3 py-1.5 text-xs">
					<span class="text-foreground font-medium">Selection</span>
					{#if selectionTotal > 0}
						<span class="text-muted-foreground">
							{selectionTotal.toLocaleString()} chunks
							{#if selectionTotal > tableHits.length}
								<span class="text-muted-foreground/70">· showing {tableHits.length}</span>
							{/if}
						</span>
					{:else}
						<span class="text-muted-foreground"
							>lasso a region, or click a legend, to list its chunks</span
						>
					{/if}

					<!-- The action the selection never had. Disabled with its reason SHOWN rather than
					     silently unavailable — a dead control with no explanation is the shape people
					     file bugs about. -->
					{#if selectionTotal > 0}
						<Button
							size="sm"
							variant="outline"
							class="h-6"
							data-testid="atlas-label-selection"
							disabled={refusal !== null}
							title={refusal ?? 'send this region into a labeling task, optionally pre-labelled'}
							onclick={() => (sendOpen = true)}
						>
							<Tags class="size-3.5" /> Label selection
						</Button>
						{#if refusal}
							<span class="text-muted-foreground/70" data-testid="atlas-send-refusal"
								>{refusal}</span
							>
						{/if}
					{/if}

					<!-- column picker -->
					<div class="relative ml-auto">
						<button
							type="button"
							class="text-muted-foreground hover:bg-muted/60 hover:text-foreground flex items-center gap-1 rounded-md px-2 py-0.5"
							onclick={() => (showColsPicker = !showColsPicker)}
							title="Choose which columns the table shows"
						>
							<Columns3 class="size-3.5" /> Columns
						</button>
						{#if showColsPicker}
							<!-- click-away backdrop -->
							<button
								type="button"
								aria-label="Close column picker"
								class="fixed inset-0 z-10 cursor-default"
								onclick={() => (showColsPicker = false)}
							></button>
							<div
								class="border-border bg-card absolute top-full right-0 z-20 mt-1 max-h-72 w-48 overflow-y-auto rounded-md border p-1 shadow-md"
							>
								{#each OFFERED_COLS as c (c.key)}
									{@const on = visibleCols.includes(c.key)}
									<button
										type="button"
										class="hover:bg-muted/60 flex w-full items-center gap-2 rounded-md px-2 py-1 text-left"
										onclick={() => toggleCol(c.key)}
									>
										<span
											class="grid size-3.5 shrink-0 place-items-center rounded-md border"
											class:border-primary={on}
											class:bg-primary={on}
											class:border-border={!on}
										>
											{#if on}<Check class="text-primary-foreground size-2.5" />{/if}
										</span>
										<span class="text-foreground">{c.label}</span>
									</button>
								{/each}
							</div>
						{/if}
					</div>
				</div>

				<div class="min-h-0 flex-1 overflow-auto">
					{#if tableHits.length}
						<HitTable
							hits={tableHits}
							{active}
							visible={visibleCols}
							query=""
							onselect={(h) => (active = h)}
						/>
					{/if}
				</div>
			</div>
		{/snippet}
	</ResizableSplit>

	<!-- `origin="atlas"` is the value this dialog has always accepted and never been given. The label
	     choices are LEFT EMPTY here: they belong to the chosen PROJECT's taxonomy, and the project is
	     picked inside the dialog. Offering the corpus's own vocabulary instead would be this surface
	     guessing at a taxonomy the server would then refuse. See the note in atlas-send.ts. -->
	<SendToProjectDialog
		bind:open={sendOpen}
		keys={sendKeys}
		dataset={activeView().datasetParam()}
		datasetVersion={activeView().rowTableVersion}
		origin="atlas"
	/>
</div>
