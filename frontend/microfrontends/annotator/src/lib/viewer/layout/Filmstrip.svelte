<script lang="ts">
	// The FILMSTRIP — your claimed items, down the left edge of the canvas.
	//
	// Reported: "did i not tell you to but gallery of itmes / bandroll on the left side in labeling
	// canvas view". Asked before and not done.
	//
	// It is the VISUAL twin of the label stream (`TaskStreamNav`), not a second idea: same set, same
	// order, same `taskCanvasHref`, both computed by `task-stream.ts`. That sharing is the point — a
	// strip that built its own list would eventually disagree with the arrows about what "next" is,
	// and the person would have two answers on one screen with no way to tell which was right.
	//
	// What it adds over the arrows is SIGHT: the next item is visible rather than remembered, and you
	// can jump three ahead instead of pressing next three times. That is why both exist.
	//
	// Anchors, not buttons: a hop is a real URL, so it stays middle-clickable and reload-restorable,
	// exactly like the arrows. Same-zone, so no `data-sveltekit-reload`.
	import { base } from '$app/paths';
	import { cn } from '@rask/ui/utils';

	import { taskCanvasHref, type StreamTask } from '../task-stream';
	import MediaThumb from './MediaThumb.svelte';

	let {
		items,
		activeTaskId,
		projectId,
	}: {
		/** The claimed stream, already filtered and ordered by `claimedStream`. */
		items: StreamTask[];
		activeTaskId: string | null;
		projectId: string;
	} = $props();

	/** The frame URL for a stream item — the same shape `review-selection.svelte.ts` and
	 *  `queue-view.ts::taskImageUrl` build: `${base}/api/chunk-frame/<key>[?dataset=]`. Three
	 *  surfaces now agree on it; a fourth spelling would be a fourth chance to drift.
	 *
	 *  The FIRST key only. An item is one thing a person decides about, so it normally has exactly
	 *  one; a multi-key item is a scope, and its first frame beats showing nothing. */
	function frameUrl(task: StreamTask): string | null {
		const key = task.source.keys?.[0];
		if (!key) return null;
		const ds = task.source.where ? `?dataset=${encodeURIComponent(task.source.where)}` : '';
		return `${base}/api/chunk-frame/${key}${ds}`;
	}

	/** Keep the open item in view as you walk the queue with the arrows or the keyboard.
	 *
	 *  An attachment rather than an `$effect` over a captured node: the element is the thing the
	 *  behaviour belongs to, and it runs when THAT element mounts — which for a keyed list is exactly
	 *  when the active item changes. `block: 'nearest'` so an item already visible does not jolt the
	 *  strip. */
	function keepInView(node: HTMLElement) {
		node.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
	}
</script>

{#if items.length > 0}
	<!-- Fixed width, its own scroll. `shrink-0` so the canvas gives up space rather than the strip:
	     a filmstrip that collapses to nothing under a narrow viewport is worse than one that scrolls,
	     because it silently removes navigation instead of making it smaller. -->
	<nav
		class="border-border bg-card/40 flex w-24 shrink-0 flex-col gap-1.5 overflow-y-auto border-r p-1.5"
		aria-label="Your claimed items"
		data-testid="filmstrip"
	>
		{#each items as task, i (task.task_id)}
			{@const active = task.task_id === activeTaskId}
			<a
				href={taskCanvasHref(task, projectId, base)}
				class={cn(
					'group relative block overflow-hidden rounded-md border transition-colors',
					active
						? 'border-primary ring-primary/40 ring-2'
						: 'border-border hover:border-muted-foreground',
				)}
				aria-current={active ? 'true' : undefined}
				title={`item ${i + 1} of ${items.length}`}
				data-testid="filmstrip-item"
				data-active={active ? 'true' : 'false'}
				{@attach active ? keepInView : () => {}}
			>
				<MediaThumb src={frameUrl(task)} kind={task.media?.kind ?? 'image'} ratio="aspect-square" alt="" />
				<!-- The ORDINAL, not the task id. Walking a queue is positional — "item 3" is what the
				     arrows and the stream control both say, and a truncated uuid would agree with
				     neither. -->
				<span
					class="bg-background/80 text-muted-foreground absolute bottom-0.5 left-0.5 rounded px-1 text-[10px] tabular-nums backdrop-blur"
				>
					{i + 1}
				</span>
			</a>
		{/each}
	</nav>
{/if}
