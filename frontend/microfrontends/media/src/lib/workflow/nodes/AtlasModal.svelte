<script lang="ts">
	/**
	 * AtlasModal — a full-screen overlay that mounts the interactive AtlasMap so
	 * a workflow Atlas node can CAPTURE a map selection.
	 *
	 * Lifecycle (the modal borrows the singleton crossFilter while open):
	 *   • on open  — pre-filter the map to the node's upstream result hits via
	 *     `crossFilter.setFilteredFromHits` (or show all points when there are
	 *     none), and wipe any stale user selection.
	 *   • interact — AtlasMap fires `onSelectionHits(hits, total)` on every
	 *     lasso / box / legend pick; we keep the latest hits in local state.
	 *   • Confirm  — hand the captured hits up via `onConfirm`.
	 *   • Cancel / close — discard via `onCancel`.
	 *   • on unmount — RESTORE the global crossFilter (clear the borrowed filter +
	 *     selection) so the /atlas page is unaffected, and let AtlasMap free its
	 *     WebGPU context. The parent only renders this component while open, so
	 *     unmount is the single cleanup point.
	 *
	 * Reuses the existing AtlasMap WebGPU scatter — it does NOT build its own.
	 */
	import { onMount } from 'svelte';
	import { X } from '@lucide/svelte';
	import AtlasMap from '$lib/atlas/AtlasMap.svelte';
	import { crossFilter } from '$lib/atlas/cross-filter.svelte';
	import { Button } from '@rask/ui/button';
	import type { Hit } from '@rask/media-api';

	let {
		upstreamHits,
		onConfirm,
		onCancel,
	}: {
		/** The node's upstream result set (last run); pre-filters the map. */
		upstreamHits: Hit[] | null;
		/** Capture the user's selection and close. */
		onConfirm: (hits: Hit[]) => void;
		/** Discard and close. */
		onCancel: () => void;
	} = $props();

	/** Latest hits AtlasMap reported for the current lasso/box/legend selection. */
	let selectedHits = $state<Hit[]>([]);
	/** Untruncated selection size (AtlasMap caps the listed hits at 1000). */
	let selectionTotal = $state(0);
	/** The dialog container — programmatically focused on open. */
	let dialogEl: HTMLDivElement | null = null;

	/** Re-parent the overlay to <body>. This component mounts INSIDE a Svelte
	 *  Flow node, i.e. inside the canvas's translated+scaled viewport — and
	 *  `position: fixed` resolves against the nearest transformed ancestor, so
	 *  without the portal the "full-screen" modal renders zoomed into the canvas
	 *  with broken pointer math. */
	function portal(node: HTMLElement): { destroy(): void } {
		document.body.appendChild(node);
		return { destroy: () => node.remove() };
	}

	onMount(() => {
		// Borrow the singleton: pre-filter to the upstream results so the user
		// lassos WITHIN them (no upstream ⇒ leave filteredIds null = all points),
		// and clear any selection the /atlas page left behind.
		crossFilter.clearSelection();
		if (upstreamHits && upstreamHits.length) crossFilter.setFilteredFromHits(upstreamHits);
		else crossFilter.clearFilter();
		dialogEl?.focus();

		// Restore the global crossFilter on unmount so /atlas is unaffected.
		return () => {
			crossFilter.clearFilter();
			crossFilter.clearSelection();
			crossFilter.showAll();
		};
	});

	function onSelectionHits(hits: Hit[], total: number): void {
		selectedHits = hits;
		selectionTotal = total;
	}

	function confirm(): void {
		onConfirm(selectedHits);
	}
</script>

<!-- Escape closes (discard) — the dialog keyboard contract. -->
<svelte:window
	onkeydown={(e) => {
	if (e.key === 'Escape') onCancel();
}}
/>

<!-- Portaled to <body> (see `portal` above) so the overlay escapes the canvas
     transform. -->
<div use:portal>
	<!-- backdrop: clicking it cancels (discard) -->
	<button
		type="button"
		aria-label="Close atlas viewer"
		class="fixed inset-0 z-30 cursor-default bg-black/50"
		onclick={onCancel}
	></button>

	<!-- content -->
	<div
		bind:this={dialogEl}
		role="dialog"
		aria-modal="true"
		aria-label="Atlas viewer"
		tabindex="-1"
		class="border-border bg-card fixed inset-4 z-40 flex flex-col overflow-hidden rounded-lg border shadow-lg outline-none"
	>
		<div class="border-border flex items-center justify-between border-b px-4 py-2">
			<div class="flex items-baseline gap-2">
				<h2 class="text-foreground text-sm font-semibold">Atlas Viewer</h2>
				<span class="text-muted-foreground text-xs">
					{#if upstreamHits && upstreamHits.length}
						pre-filtered to {upstreamHits.length.toLocaleString()} upstream hits
					{:else}
						all points
					{/if}
					· lasso a region, then confirm
				</span>
			</div>
			<button
				type="button"
				class="text-muted-foreground hover:bg-muted/60 hover:text-foreground rounded-md p-1 transition-colors"
				title="Close (discard)"
				aria-label="Close atlas viewer"
				onclick={onCancel}
			>
				<X class="size-4" />
			</button>
		</div>

		<div class="min-h-0 flex-1">
			<AtlasMap {onSelectionHits} />
		</div>

		<div class="border-border flex items-center justify-between border-t px-4 py-2">
			<span class="text-muted-foreground text-xs">
				{#if selectionTotal > 0}
					<span class="text-foreground">{selectionTotal.toLocaleString()}</span> points selected
				{:else}
					No selection yet
				{/if}
			</span>
			<div class="flex items-center gap-2">
				<Button variant="ghost" size="sm" onclick={onCancel}>Cancel</Button>
				<Button size="sm" disabled={selectionTotal === 0} onclick={confirm}>Confirm selection</Button>
			</div>
		</div>
	</div>
</div>
