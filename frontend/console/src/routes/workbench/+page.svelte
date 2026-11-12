<script lang="ts">
	/**
	 * THE LAKEHOUSE WORKBENCH — a dock beside the catalog's areas rather than inside one, because it
	 * composes them.
	 *
	 * The panels are the console's own components: Events reads the workbench's one lineage feed,
	 * Tables is the very registry `/catalog/tables` renders. Nothing here is a mirror.
	 *
	 * The arrangement is restored once, autosaved after every change, and can be saved as a named
	 * view; where both are kept is `#lib/dock/store.ts`'s decision alone (this browser).
	 */
	import { onMount } from 'svelte';
	import type { LayoutConfig } from 'horizon-layout';
	import { Button } from '@rask/ui/button';
	import { auth } from '#lib/auth.svelte.ts';
	import Dock from '#lib/dock/Dock.svelte';
	import ViewSidebar from '#lib/dock/ViewSidebar.svelte';
	import { eventFeed } from '#lib/dock/bench.svelte.ts';
	import { defaultLayout } from '#lib/dock/layout.ts';
	import { startWorkbench, type WorkbenchSession } from '#lib/dock/session.ts';
	import { openWorkbenchStore } from '#lib/dock/store.ts';

	let layout = $state<LayoutConfig | null>(null);
	let session = $state.raw<WorkbenchSession | null>(null);
	/** Why the stored layout could not be read; while set, nothing is saved over it. */
	let unreadable = $state<string | null>(null);

	/** The layout as last restored or saved, so the restored value is not written back. */
	let last: string | null = null;

	const store = $derived(session?.store ?? null);
	const autosave = $derived(session?.autosave ?? null);
	const views = $derived(session?.views ?? null);

	onMount(() => {
		void eventFeed.poll();
		let left = false;
		const opening = openWorkbenchStore(auth.enabled ? (auth.session?.sub ?? null) : null);
		void startWorkbench(
			opening,
			{
				snapshot: () => (layout ? $state.snapshot(layout) : null),
				onLayout: (read, wasRefused) => {
					unreadable = read.status === 'unreadable' ? read.detail : null;
					// Saving was off over an unreadable layout and what is stored is readable now (another
					// tab reset it): show that instead of this page's default, rather than save over it.
					if (wasRefused && read.status === 'ok') {
						last = JSON.stringify(read.layout);
						layout = read.layout;
					}
				},
				started: (started) => {
					const read = started.restored;
					// An unreadable layout shows the default and is never saved over until the person resets it.
					unreadable = read.status === 'unreadable' ? read.detail : null;
					layout = read.status === 'ok' ? read.layout : defaultLayout();
					session = started;
				},
			},
			() => left,
		);
		return () => {
			left = true;
			session?.stop();
		};
	});

	// Every change after the restore: the named view re-checks its modified marker, and the draft is
	// saved. The restored value itself is not written back.
	$effect(() => {
		if (layout === null || autosave === null) return;
		const snapshot = $state.snapshot(layout);
		const serialized = JSON.stringify(snapshot);
		if (last === null || serialized === last) {
			last = serialized;
			return;
		}
		last = serialized;
		views?.touch();
		autosave.schedule(snapshot);
	});

	/** Apply a saved view; one this workbench cannot show leaves the arrangement as it is, and says why. */
	function applyView(id: string): void {
		const read = views?.select(id);
		if (read === undefined) return;
		if (read.status === 'unreadable') {
			views?.report(`This view could not be applied: ${read.detail}.`);
			return;
		}
		if (read.status !== 'ok') return;
		layout = read.layout;
		views?.activate(id);
	}

	/** The person's explicit choice to replace a stored layout that could not be read. */
	function resetLayout(): void {
		const fresh = defaultLayout();
		autosave?.reset(fresh);
		unreadable = null;
		last = JSON.stringify(fresh);
		layout = fresh;
		void views?.refresh();
	}
</script>

<svelte:head><title>Workbench · rask</title></svelte:head>
<!-- Leaving or reloading the page writes a save the debounce still holds. -->
<svelte:window onpagehide={() => autosave?.flush()} />

<!-- The page's own heading: the dock's panels head themselves one level under it. Not drawn, since
     the breadcrumb already names the page and the dock needs the height. -->
<h1 class="sr-only">Workbench</h1>
<div class="wrap">
	{#if views}
		<ViewSidebar {views} onselect={applyView} />
	{/if}
	<div class="dock">
		{#if unreadable}
			<p class="note problem" role="status">
				<span>
					Your saved layout could not be read ({unreadable}). It has not been changed; the default
					is shown and changes are not saved.
				</span>
				<Button variant="outline" size="xs" onclick={resetLayout}>Reset layout</Button>
			</p>
		{:else if store && !store.persistent}
			<p class="note" role="status">
				This browser keeps no storage for this site: the layout and views last until the tab closes.
			</p>
		{/if}
		{#if layout}
			<Dock bind:layout />
		{/if}
	</div>
</div>

<style>
	.wrap {
		display: flex;
		flex: 1 1 0;
		min-height: 0;
		width: 100%;
	}
	/* Below a tablet's width the views rail moves above the dock, so the panels keep the full width
	   rather than a sliver beside a fixed-width rail. */
	@media (width < 768px) {
		.wrap {
			flex-direction: column;
		}
	}
	/* The dock fills a sized flex item; `min-height: 0` lets it shrink below its content. The stacking
	   context keeps anything the dock positions from painting over the shared navbar's dropdowns. */
	.dock {
		display: flex;
		flex: 1 1 0;
		flex-direction: column;
		min-height: 0;
		min-width: 0;
		position: relative;
		z-index: 0;
		isolation: isolate;
	}
	.note {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.75rem;
		margin: 0;
		padding: 0.375rem 0.75rem;
		border-bottom: 1px solid var(--border);
		color: var(--muted-foreground);
		font-size: 0.75rem;
	}
	.note.problem {
		color: var(--destructive);
	}
</style>
