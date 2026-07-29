<script lang="ts">
	/**
	 * Hover/click ANALYSIS popover for the atlas — what turns the map from a
	 * viewer into a cluster-discovery + data-inspection tool.
	 *
	 * Cluster id and the declared categorical channel values come INSTANTLY from
	 * the point arrays; the title/body/caption (read through the dataset view) are
	 * lazy-fetched via `getAtlasChunk` on a ~150ms hover-settle debounce and
	 * memoised in a tiny LRU keyed by point index. Absolutely positioned
	 * (bg-card/85 backdrop-blur) to match the toolbar/legend overlays.
	 */
	import { getAtlasChunk, type AtlasPoints } from '@rask/media-api';
	import { activeView } from '@rask/media-api/descriptor';

	let {
		pts,
		index,
		x,
		y,
	}: {
		pts: AtlasPoints;
		/** Hovered point index, or null when nothing is hovered. */
		index: number | null;
		/** Screen position (px, relative to the map container) of the point. */
		x: number;
		y: number;
	} = $props();

	const view = activeView();

	// ── instant fields from the arrays (no fetch) ─────────────────────────────
	const clusterId = $derived(index != null ? (pts.cluster?.[index] ?? null) : null);

	/** Each declared colour channel's value at the hovered point (empty dropped) —
	 *  the generic replacement for the old hardcoded language/name/video lines. */
	const channelChips = $derived.by((): { name: string; value: string }[] => {
		const i = index;
		if (i == null) return [];
		const out: { name: string; value: string }[] = [];
		for (const [name, ch] of Object.entries(pts.channels)) {
			const code = ch.codes[i];
			const value = code != null ? (ch.labels[code] ?? '') : '';
			if (value) out.push({ name, value });
		}
		return out;
	});

	/** The point's identity key path (descriptor key order) for the lazy fetch. */
	function keyAt(i: number): (string | number)[] | null {
		const path: (string | number)[] = [];
		for (const field of view.keyFields) {
			if (field === view.docKeyField) {
				const dc = pts.doc[i];
				const doc = dc !== undefined ? pts.docs[dc] : undefined;
				if (doc === undefined) return null;
				path.push(doc);
			} else {
				const v = pts.keys[field]?.[i];
				if (v === undefined) return null;
				path.push(v);
			}
		}
		return path;
	}

	// ── lazy title/body/caption snippet (debounced fetch + LRU) ───────────────
	const LRU_MAX = 200;
	type Snip = { title: string; body: string; caption: string };
	const cache = new Map<number, Snip>();
	let snip = $state<Snip | null>(null);
	let snipFor = $state<number | null>(null);

	function remember(i: number, s: Snip) {
		cache.delete(i);
		cache.set(i, s);
		if (cache.size > LRU_MAX) {
			const oldest = cache.keys().next().value;
			if (oldest !== undefined) cache.delete(oldest);
		}
	}

	$effect(() => {
		const i = index;
		if (i == null) {
			snip = null;
			snipFor = null;
			return;
		}
		const cached = cache.get(i);
		if (cached !== undefined) {
			snip = cached;
			snipFor = i;
			return;
		}
		snip = null;
		snipFor = null;
		const key = keyAt(i);
		if (!key) return;
		const timer = setTimeout(async () => {
			try {
				const hit = await getAtlasChunk(key);
				const s: Snip = {
					title: view.title(hit),
					body: view.body(hit).slice(0, 240),
					caption: (view.caption(hit) ?? '').slice(0, 200),
				};
				remember(i, s);
				// Only apply if still hovering the same point.
				if (index === i) {
					snip = s;
					snipFor = i;
				}
			} catch {
				/* snip stays null — instant fields still show */
			}
		}, 150);
		return () => clearTimeout(timer);
	});

	// Once the title is known, drop the channel chip that merely repeats it (for
	// this corpus the title IS a channel), so it shows once as the header.
	const chips = $derived(
		snip && snipFor === index ? channelChips.filter((c) => c.value !== snip!.title) : channelChips,
	);
</script>

{#if index != null}
	<div
		class="border-border bg-card/85 pointer-events-none absolute z-20 w-64 max-w-[80vw] -translate-x-1/2 -translate-y-full rounded-md border p-2.5 text-xs shadow-md backdrop-blur"
		style:left="{x}px"
		style:top="{Math.max(8, y - 10)}px"
	>
		<div class="mb-1 flex flex-wrap items-center gap-1.5">
			{#if clusterId != null}
				<span class="bg-secondary/70 text-foreground rounded-md px-1.5 py-0.5 font-mono">
					{clusterId < 0 ? 'noise' : `cluster #${clusterId}`}
				</span>
			{/if}
			{#each chips as ch (ch.name)}
				<span
					class="bg-secondary/40 text-muted-foreground truncate rounded-md px-1.5 py-0.5"
					title={ch.value}
				>
					{ch.value}
				</span>
			{/each}
		</div>
		{#if snip && snipFor === index}
			{#if snip.title}
				<div class="text-foreground mb-1 truncate font-medium" title={snip.title}>{snip.title}</div>
			{/if}
			{#if snip.body}
				<div class="text-muted-foreground line-clamp-2">{snip.body}</div>
			{/if}
			{#if snip.caption}
				<div class="text-foreground/80 mt-1 line-clamp-2 italic" title={snip.caption}>
					<span class="text-muted-foreground/70">caption:</span>
					{snip.caption}
				</div>
			{/if}
			{#if !snip.body && !snip.caption}
				<div class="text-muted-foreground/60 italic">no text</div>
			{/if}
		{:else}
			<div class="text-muted-foreground/60 italic">loading…</div>
		{/if}
		<div class="text-muted-foreground/60 mt-1 text-[0.7rem]">click to play</div>
	</div>
{/if}
