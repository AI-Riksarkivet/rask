<script lang="ts">
	/** A clickable list of result hits (thumbnail + name + snippet). Clicking the
	 *  row plays it in the Inspector; each row also carries inline tags you can add
	 *  or remove anywhere a hit appears (shared tag store → flows into Export).
	 *  Shared by the Results node and the Inspector so both render identically. */
	import { Play, Plus, X } from '@lucide/svelte';
	import { activeView, chunkFrameUrl, type Hit } from '@rask/explorer-api';
	import { graph } from '$lib/workflow/graph.svelte';
	import { hitKey } from '$lib/utils';

	let { hits, maxHeight = 'max-h-72' }: { hits: Hit[]; maxHeight?: string } = $props();

	const selectedKey = $derived(graph.selectedHit ? hitKey(graph.selectedHit) : null);

	// Which row's add-tag input is open (one at a time keeps the list compact).
	let addingKey = $state<string | null>(null);
	let draft = $state('');

	function openAdd(key: string): void {
		addingKey = key;
		draft = '';
	}

	function commitAdd(h: Hit): void {
		const t = draft.trim();
		if (t) graph.tags.addTo([h], [t]); // add-only union (won't remove an existing tag)
		draft = '';
		addingKey = null;
	}
</script>

<div class="nodrag nowheel flex {maxHeight} flex-col gap-1.5 overflow-y-auto pr-1">
	{#each hits as h (hitKey(h))}
		{@const key = hitKey(h)}
		{@const isSel = selectedKey === key}
		{@const tags = graph.tags.forHit(h)}
		{@const title = activeView().title(h)}
		{@const body = activeView().body(h)}
		<div
			class="group bg-background rounded-md border transition-colors"
			class:border-primary={isSel}
			class:border-border={!isSel}
		>
			<button
				type="button"
				onclick={(e) => {
	// Don't let the click bubble to the node wrapper (would fire
	// onnodeclick → inspectNode and clear the hit we just selected).
	e.stopPropagation();
	graph.selectHit(h);
}}
				class="hover:bg-muted flex w-full gap-2 p-1.5 text-left transition-colors"
			>
				<div class="relative shrink-0">
					<img
						src={chunkFrameUrl(h)}
						alt=""
						loading="lazy"
						class="bg-muted h-10 w-14 rounded-md object-cover"
						onerror={(e) => {
	// Many transcript chunks have no extracted frame → 404. Swap to a
	// transparent pixel once so it shows a clean muted box, not a
	// broken-image glyph (and never re-requests).
	const img = e.currentTarget as HTMLImageElement;
	if (img.dataset.fallback) return;
	img.dataset.fallback = '1';
	img.src =
		'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';
}}
					/>
					<span
						class="pointer-events-none absolute inset-0 grid place-items-center rounded-md bg-black/45 opacity-0 transition-opacity group-hover:opacity-100"
					>
						<Play class="size-4 text-white" />
					</span>
				</div>
				<div class="min-w-0">
					{#if title}
						<div class="text-foreground truncate text-[0.7rem] font-medium" {title}>
							{title}
						</div>
					{/if}
					<div class="text-muted-foreground line-clamp-2 text-[0.7rem]">{body}</div>
				</div>
			</button>

			<!-- Inline tags (click ✕ to remove; ＋ to add) — these write the shared
           tag store, so the same chunk shows the tag everywhere and Export picks
           it up. Stop propagation so tag clicks never trigger play/inspect. -->
			<div class="flex flex-wrap items-center gap-1 px-1.5 pb-1.5">
				{#each tags as tag (tag)}
					<span
						class="bg-primary/10 text-foreground inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[9px]"
					>
						{tag}
						<button
							type="button"
							class="text-muted-foreground hover:text-destructive"
							aria-label="Remove tag {tag}"
							onclick={(e) => {
	e.stopPropagation();
	graph.tags.toggle(h, tag);
}}
						>
							<X class="size-2.5" />
						</button>
					</span>
				{/each}
				{#if addingKey === key}
					<!-- svelte-ignore a11y_autofocus -->
					<input
						class="border-border bg-background text-foreground focus:border-primary w-24 rounded-md border px-1 py-0.5 text-[9px] outline-none"
						placeholder="tag…"
						autofocus
						bind:value={draft}
						onclick={(e) => e.stopPropagation()}
						onkeydown={(e) => {
	e.stopPropagation();
	if (e.key === 'Enter') commitAdd(h);
	else if (e.key === 'Escape') {
		// Clear the draft BEFORE unmounting so the blur fired by removal
		// doesn't re-commit what was typed (Escape = cancel).
		draft = '';
		addingKey = null;
	}
}}
						onblur={() => commitAdd(h)}
					/>
				{:else}
					<button
						type="button"
						class="border-border text-muted-foreground hover:bg-muted hover:text-foreground inline-flex items-center gap-0.5 rounded-md border border-dashed px-1 py-0.5 text-[9px]"
						onclick={(e) => {
	e.stopPropagation();
	openAdd(key);
}}
					>
						<Plus class="size-2.5" />
						tag
					</button>
				{/if}
			</div>
		</div>
	{/each}
</div>
