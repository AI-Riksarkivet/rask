<script lang="ts">
	// Drill-in for ONE document: its chunk timeline (the annotatable media units),
	// each openable in the canvas — or the whole document as a review selection.
	// Chunks come from the viewer's /api/doc-chunks (ordered, key fields + times
	// + body); frames render lazily via /api/chunk-frame and hide themselves when a
	// chunk has no extracted frame yet.
	import { onMount } from 'svelte';
	import { ArrowLeft, ListChecks, Pencil } from '@lucide/svelte';
	import { getDocChunks, type DocChunk, type Document } from '@rask/explorer-api';
	import type { DatasetView } from '@rask/explorer-api/descriptor';
	import { Button } from '@rask/ui/button';

	import SimilarPanel from './SimilarPanel.svelte';

	let {
		view,
		doc,
		onopen,
		onselect,
		selectedKeys = [],
		onback,
	}: {
		view: DatasetView;
		doc: Document;
		/** Open these key-paths (`doc/speech/chunk`) in the annotate canvas. */
		onopen: (keys: string[]) => void;
		/** Offer these keys for a BULK send rather than opening them. Optional: a surface that only
		 *  views (the canvas hand-off) passes nothing and the control is absent. */
		onselect?: ((keys: string[]) => void) | undefined;
		/** What is already selected — so "more like this" can report how many neighbours are NEW
		 *  rather than how many it found. */
		selectedKeys?: string[];
		onback: () => void;
	} = $props();

	let chunks = $state<DocChunk[] | null>(null);
	let error = $state<string | null>(null);

	const title = $derived(view.title(doc));
	// Doc-chunk rows carry only the NON-doc identity fields (the backend strips the
	// doc key — it's the path parameter), so stamp the document's own key back in before
	// building the media key-path. Found live: without this the key renders as `/0/0`.
	const keyOf = (chunk: DocChunk): string =>
		view.keyPath({ ...chunk, [view.docKeyField]: view.docId(doc) }).join('/');

	// One fetch per opened document — the parent unmounts this picker (back → grid)
	// before another document can open, so mount-time is the whole lifecycle.
	onMount(() => {
		getDocChunks(view.docId(doc))
			.then((t) => (chunks = t.chunks))
			.catch((e: unknown) => (error = e instanceof Error ? e.message : String(e)));
	});

	function fmtTime(s: number): string {
		const m = Math.floor(s / 60);
		return `${m}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
	}
</script>

<section class="flex h-full min-h-0 flex-col gap-3" data-testid="chunk-picker">
	<div class="flex shrink-0 items-center gap-2">
		<Button variant="ghost" size="sm" onclick={onback}>
			<ArrowLeft class="size-4" /> Documents
		</Button>
		<h2 class="min-w-0 truncate text-base font-medium" {title}>{title}</h2>
		{#if chunks?.length}
			<Button
				variant="default"
				size="sm"
				class="ml-auto shrink-0"
				data-testid="review-all"
				onclick={() => onopen(chunks!.map(keyOf))}
			>
				<ListChecks class="size-4" /> Review all {chunks.length} chunks
			</Button>
			<!-- SELECT rather than open. The same keys, offered to the bulk-send bar instead of the
			     canvas — "review all" is one person reading; this is queueing them for a team. -->
			{#if onselect}
				<Button
					variant="outline"
					size="sm"
					class="shrink-0"
					data-testid="select-all-for-send"
					onclick={() => onselect?.(chunks!.map(keyOf))}
				>
					Select for sending
				</Button>
			{/if}
		{/if}
	</div>

	{#if error}
		<p class="text-destructive text-sm">Failed to load chunks: {error}</p>
	{:else if chunks === null}
		<p class="text-muted-foreground text-sm">Loading chunks…</p>
	{:else if chunks.length === 0}
		<p class="text-muted-foreground text-sm">This document has no chunks to annotate.</p>
	{:else}
		<ul class="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto pr-1">
			{#each chunks as chunk, i (keyOf(chunk))}
				{@const time = view.time(chunk)}
				<li>
					<!-- Card geometry lifted from @rask/ui's Card (rounded-lg, border, bg-card,
					     shadow-sm) so a chunk row and a data-zone card are the same object. -->
					<button
						type="button"
						data-testid="chunk-row"
						class="border-border bg-card text-card-foreground hover:border-primary hover:bg-muted/40 flex w-full items-center gap-3 rounded-lg border p-2 text-left shadow-sm transition-colors"
						onclick={() => onopen([keyOf(chunk)])}
					>
						<img
							src={view.frameUrl({ ...chunk, [view.docKeyField]: view.docId(doc) })}
							alt=""
							loading="lazy"
							class="bg-muted h-12 w-20 shrink-0 rounded-md object-cover"
							onerror={(e) => ((e.currentTarget as HTMLImageElement).style.visibility = 'hidden')}
						/>
						<div class="min-w-0 flex-1">
							<div class="text-muted-foreground truncate font-mono text-xs">
								#{i + 1}
								{#if time}
									· {fmtTime(time.start)}–{fmtTime(time.end)}
								{/if}
								· {keyOf(chunk)}
							</div>
							<div class="line-clamp-2 text-sm">{view.body(chunk)}</div>
						</div>
						<Pencil class="text-muted-foreground size-4 shrink-0" />
					</button>

					<!-- Build a batch FROM this chunk. Only where a selection is being built: on the
					     canvas hand-off (`onselect` absent) there is nothing to add neighbours to, and
					     an "Add 24 to selection" with no selection is a control that does nothing. -->
					{#if onselect}
						<SimilarPanel
							seedKey={keyOf(chunk)}
							keyFields={view.keyFields}
							dataset={view.id}
							selected={selectedKeys}
							onadd={(keys) => onselect?.(keys)}
						/>
					{/if}
				</li>
			{/each}
		</ul>
	{/if}
</section>
