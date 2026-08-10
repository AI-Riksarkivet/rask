<script lang="ts">
	// Bottom page-navigation bar. Controlled: renders a pages list + current index
	// and emits navigate. Document-plane today (pages); for audio/video this becomes
	// a segment/scene strip — same controlled contract. (Ported from ra-anno PageNavBar.)
	import { ChevronLeft, ChevronRight, Columns2 } from '@lucide/svelte';
	import { Button } from '@rask/ui/button';
	import { cn } from '@rask/ui/utils';

	export interface PageRef {
		key: string;
		label: string;
	}

	let {
		pages,
		current = 0,
		onNavigate,
		compareHref = null,
		class: className,
	}: {
		pages: PageRef[];
		current?: number;
		onNavigate?: (index: number) => void;
		/** When the item has candidates to judge BETWEEN, the bar offers the compare view — the
		 *  side-by-side twin of this one-at-a-time walk. Null hides the affordance. */
		compareHref?: string | null;
		class?: string;
	} = $props();

	const total = $derived(pages.length);
	const go = (i: number) => {
		if (i >= 0 && i < total) onNavigate?.(i);
	};
</script>

<!-- Floating over the canvas: a card surface at the estate's rounded-lg + shadow-sm, matching
     the zoom cluster and the assist bar so the three read as one family of overlays. -->
<nav
	class={cn(
	'border-border bg-card/90 text-card-foreground pointer-events-auto absolute bottom-2 left-1/2 z-10 flex -translate-x-1/2 items-center gap-1 rounded-lg border p-1 shadow-sm backdrop-blur',
	className,
)}
	data-testid="page-nav"
>
	<Button
		variant="ghost"
		size="icon-xs"
		title="Previous page"
		disabled={current <= 0}
		onclick={() => go(current - 1)}
	>
		<ChevronLeft class="size-4" />
	</Button>
	<span class="text-muted-foreground px-1 text-xs tabular-nums">
		{total === 0 ? '0 / 0' : `${current + 1} / ${total}`}
	</span>
	<Button
		variant="ghost"
		size="icon-xs"
		title="Next page"
		disabled={current >= total - 1}
		onclick={() => go(current + 1)}
	>
		<ChevronRight class="size-4" />
	</Button>
	{#if compareHref !== null}
		<Button
			variant="ghost"
			size="icon-xs"
			title="Compare candidates side by side"
			href={compareHref}
			data-testid="enter-compare"
		>
			<Columns2 class="size-4" />
		</Button>
	{/if}
</nav>
