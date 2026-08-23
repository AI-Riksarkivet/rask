<script lang="ts">
	import { untrack } from 'svelte';
	import type { Snippet } from 'svelte';

	/**
	 * Two-pane split with a draggable divider. `orientation='horizontal'` lays the
	 * panes side-by-side (left|right); `'vertical'` stacks them (left=top,
	 * right=bottom). The split fraction (0..1, of the first pane) is persisted to
	 * localStorage so the user's choice survives reloads.
	 */

	type Props = {
		left: Snippet;
		right: Snippet;
		/** Storage key — change to keep separate splits across pages. */
		storageKey?: string;
		/** Initial fraction of the FIRST pane (left/top), 0..1. */
		initial?: number;
		/** Hard min size (px, along the split axis) of the first pane. */
		minLeft?: number;
		/** Hard min size (px, along the split axis) of the second pane. */
		minRight?: number;
		/** Split axis: side-by-side or stacked. */
		orientation?: 'horizontal' | 'vertical';
	};
	let {
		left,
		right,
		storageKey = 'lance-media-split',
		initial = 0.6,
		minLeft = 360,
		minRight = 320,
		orientation = 'horizontal',
	}: Props = $props();

	const vertical = $derived(orientation === 'vertical');

	let container = $state<HTMLDivElement | null>(null);
	// Read `initial` once at component creation; `fraction` is the live state.
	let fraction = $state<number>(untrack(() => initial));

	// Hydrate persisted fraction once mounted (don't run on the server).
	$effect(() => {
		try {
			const v = localStorage.getItem(storageKey);
			if (v !== null) {
				const f = parseFloat(v);
				if (!Number.isNaN(f) && f > 0 && f < 1) fraction = f;
			}
		} catch {}
	});

	let dragging = $state(false);

	// Drag geometry is cached at pointerdown and fraction writes are coalesced
	// to one per animation frame: pointermove fires far above frame rate on
	// high-Hz mice, and EVERY fraction write re-lays-out both panes — on the
	// Tree page that's a full d3 treemap/sankey layout plus a few-hundred-cell
	// SVG rewrite, which made dragging the divider visibly laggy. Per-event
	// getBoundingClientRect also forces a reflow; the rect can't change
	// mid-drag (the container is the page split itself).
	let dragRect: DOMRect | null = null;
	let rafId = 0;
	let pendingPos = 0;

	function onPointerDown(e: PointerEvent) {
		if (!container) return;
		dragging = true;
		dragRect = container.getBoundingClientRect();
		(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
		e.preventDefault();
	}

	function onPointerMove(e: PointerEvent) {
		if (!dragging || !dragRect) return;
		pendingPos = vertical ? e.clientY - dragRect.top : e.clientX - dragRect.left;
		if (rafId) return; // a frame is already scheduled — just update the target
		rafId = requestAnimationFrame(() => {
			rafId = 0;
			if (!dragRect) return; // drag ended before the frame fired
			const total = vertical ? dragRect.height : dragRect.width;
			const minF = minLeft / total;
			const maxF = 1 - minRight / total;
			fraction = Math.max(minF, Math.min(maxF, pendingPos / total));
		});
	}

	function onPointerUp(e: PointerEvent) {
		if (!dragging) return;
		dragging = false;
		if (rafId) {
			// Flush (don't drop) the pending frame: the divider must settle at the
			// exact release point, and that position is what persists below.
			cancelAnimationFrame(rafId);
			rafId = 0;
			if (dragRect) {
				const total = vertical ? dragRect.height : dragRect.width;
				const minF = minLeft / total;
				const maxF = 1 - minRight / total;
				fraction = Math.max(minF, Math.min(maxF, pendingPos / total));
			}
		}
		dragRect = null;
		(e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
		try {
			localStorage.setItem(storageKey, fraction.toFixed(3));
		} catch {}
	}

	// Double-click resets to default
	function onDoubleClick() {
		fraction = initial;
		try {
			localStorage.setItem(storageKey, initial.toFixed(3));
		} catch {}
	}
</script>

<div
	bind:this={container}
	class="grid h-full min-h-0"
	style:grid-template-columns={vertical ? undefined : `${(fraction * 100).toFixed(2)}% 6px 1fr`}
	style:grid-template-rows={vertical ? `${(fraction * 100).toFixed(2)}% 6px 1fr` : undefined}
	class:cursor-col-resize={dragging && !vertical}
	class:cursor-row-resize={dragging && vertical}
	class:select-none={dragging}
>
	<div class="min-h-0 overflow-hidden">{@render left()}</div>

	<button
		type="button"
		aria-label="Resize panels"
		onpointerdown={onPointerDown}
		onpointermove={onPointerMove}
		onpointerup={onPointerUp}
		ondblclick={onDoubleClick}
		onkeydown={(e) => {
			// Same px clamps as the pointer path (0.2/0.8 ignored minLeft/minRight
			// and could violate them), and persist like a drag does.
			if (!container) return;
			const dec = vertical ? 'ArrowUp' : 'ArrowLeft';
			const inc = vertical ? 'ArrowDown' : 'ArrowRight';
			if (e.key !== dec && e.key !== inc) return;
			const rect = container.getBoundingClientRect();
			const total = vertical ? rect.height : rect.width;
			const minF = minLeft / total;
			const maxF = 1 - minRight / total;
			const next = e.key === dec ? fraction - 0.02 : fraction + 0.02;
			fraction = Math.max(minF, Math.min(maxF, next));
			try {
				localStorage.setItem(storageKey, fraction.toFixed(3));
			} catch {}
		}}
		title="Drag to resize · double-click to reset"
		class="group border-border bg-secondary/40 hover:bg-primary/30 active:bg-primary/40 focus-visible:bg-primary/40 relative
           flex items-center justify-center
           transition-colors focus-visible:outline-none"
		class:cursor-col-resize={!vertical}
		class:cursor-row-resize={vertical}
		class:border-x={!vertical}
		class:border-y={vertical}
	>
		<!-- Persistent grip dots so it's obvious the bar is draggable -->
		<span
			aria-hidden="true"
			class="text-muted-foreground/70 group-hover:text-foreground flex gap-0.5"
			class:flex-col={!vertical}
			class:flex-row={vertical}
		>
			<span class="size-0.5 rounded-full bg-current"></span>
			<span class="size-0.5 rounded-full bg-current"></span>
			<span class="size-0.5 rounded-full bg-current"></span>
			<span class="size-0.5 rounded-full bg-current"></span>
			<span class="size-0.5 rounded-full bg-current"></span>
		</span>
		<!-- Hover tooltip that confirms the affordance -->
		<span
			class="border-border bg-card text-muted-foreground pointer-events-none absolute top-1/2 left-1/2
                 -translate-x-1/2 translate-y-6 rounded border px-2 py-0.5 text-[10px] whitespace-nowrap opacity-0
                 transition-opacity group-hover:opacity-100"
		>
			drag to resize
		</span>
	</button>

	<div class="min-h-0 overflow-hidden">{@render right()}</div>
</div>
