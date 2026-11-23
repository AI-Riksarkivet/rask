<script lang="ts">
	/**
	 * The workbench's dock: `horizon-layout` over the console's panels, with the controls the library
	 * leaves to its host — close a tab, open a closed panel, maximize and restore, pop out.
	 *
	 * The whole arrangement is the one bound `layout` value; every control here writes a new value
	 * through `layout.ts`, and the library validates each against `views` before it draws it.
	 *
	 * THE PANEL RULE: the library renders only the active tab of a group and remounts a panel when it
	 * is moved, maximized or popped out, so a panel holds nothing in component state that must survive
	 * that. Its data lives in a module or a shared read instance (`bench.svelte.ts`, `fetchTables`);
	 * scroll position is lost and that is accepted.
	 */
	import { HorizonLayout, type Id, type LayoutConfig, type View } from 'horizon-layout';
	import { SvelteMap } from 'svelte/reactivity';
	import { ExternalLink, Maximize2, Minimize2, Plus, X } from '@lucide/svelte';
	import { Button } from '@riksarkivet/ui/button';
	import * as DropdownMenu from '@riksarkivet/ui/dropdown-menu';
	import EventsPanel from './panels/EventsPanel.svelte';
	import TablesPanel from './panels/TablesPanel.svelte';
	import {
		PANELS,
		closePanel,
		closedPanels,
		isPanelId,
		openPanel,
		popOut,
		type PanelId,
	} from './layout.ts';

	let { layout = $bindable() }: { layout: LayoutConfig } = $props();

	const titleOf = (id: Id): string => PANELS.find((p) => p.id === id)?.title ?? id;

	const closed = $derived(closedPanels(layout));
	const maximized = $derived(layout.maximizedView);

	function close(id: Id): void {
		layout = closePanel(layout, id);
	}

	function open(id: PanelId, beside?: Id): void {
		layout = openPanel(layout, id, beside);
	}

	function maximize(id: Id): void {
		layout = { ...layout, maximizedView: id };
	}

	function restore(): void {
		const next = { ...layout };
		delete next.maximizedView;
		layout = next;
	}

	// Top-level snippets, so the script can hand them to the library. One map for the dock's life:
	// the library re-validates the layout whenever `views` changes.
	const views = new SvelteMap<Id, View>([
		['tables', { title: titleOf('tables'), snippet: tables, tabControls: [closeTab] }],
		['events', { title: titleOf('events'), snippet: events, tabControls: [closeTab] }],
	]);

	/** A closed popout window returns its panel to the dock. The library has already dropped it from
	 *  `popouts`; closing it first keeps this correct whichever value the binding holds now. */
	function returnFromPopout(id: Id): void {
		if (isPanelId(id)) layout = openPanel(closePanel(layout, id), id);
	}
</script>

<!-- A popped-out panel is mounted in another document: horizon-layout copies the page's stylesheets
     into it and mirrors `<html>`'s class, `data-theme` and inline custom properties, so the panel
     follows the person's theme, mode and accent there too. -->
{#snippet tables()}
	<div class="rask-dock-panel"><TablesPanel /></div>
{/snippet}
{#snippet events()}
	<div class="rask-dock-panel"><EventsPanel /></div>
{/snippet}

{#snippet closeTab(id: Id)}
	<Button
		variant="ghost"
		size="icon-xs"
		class="size-4 opacity-60 hover:opacity-100 focus-visible:opacity-100"
		aria-label="Close {titleOf(id)}"
		title="Close {titleOf(id)}"
		onclick={(e: MouseEvent) => {
			e.stopPropagation();
			close(id);
		}}
	>
		<X class="size-3" />
	</Button>
{/snippet}

{#snippet panelPicker(beside?: Id)}
	<DropdownMenu.Root>
		<DropdownMenu.Trigger disabled={closed.length === 0}>
			{#snippet child({ props })}
				<Button
					{...props}
					variant="ghost"
					size="icon-xs"
					aria-label="Open a panel"
					title={closed.length === 0 ? 'Every panel is open' : 'Open a panel'}
				>
					<Plus />
				</Button>
			{/snippet}
		</DropdownMenu.Trigger>
		<DropdownMenu.Content class="min-w-36" align="end">
			<DropdownMenu.Label class="text-muted-foreground text-xs">Open a panel</DropdownMenu.Label>
			{#each closed as id (id)}
				<DropdownMenu.Item onSelect={() => open(id, beside)}>{titleOf(id)}</DropdownMenu.Item>
			{/each}
		</DropdownMenu.Content>
	</DropdownMenu.Root>
{/snippet}

{#snippet groupActions(active: Id)}
	<div class="flex items-center gap-0.5">
		{@render panelPicker(active)}
		<Button
			variant="ghost"
			size="icon-xs"
			aria-label="Maximize {titleOf(active)}"
			title="Maximize {titleOf(active)}"
			onclick={() => maximize(active)}
		>
			<Maximize2 />
		</Button>
		<Button
			variant="ghost"
			size="icon-xs"
			aria-label="Pop out {titleOf(active)}"
			title="Pop out {titleOf(active)} into its own window"
			onclick={() => (layout = popOut(layout, active))}
		>
			<ExternalLink />
		</Button>
	</div>
{/snippet}

{#if maximized}
	<div class="rask-dock-maximized-bar">
		<span class="text-sm font-medium">{titleOf(maximized)}</span>
		<Button variant="ghost" size="xs" onclick={restore}>
			<Minimize2 /> Restore
		</Button>
	</div>
{/if}

<div class="rask-dock-frame">
	<!-- Mounted whenever the dock is: the library owns the popout windows and closes every one of them
	     when it unmounts, so an empty dock with panels in their own windows must keep it. -->
	<HorizonLayout
		bind:config={layout}
		{views}
		tabgroupControls={[groupActions]}
		onPopoutClose={returnFromPopout}
	/>
	{#if !layout.root && !maximized}
		<div class="rask-dock-empty">
			<p class="text-muted-foreground text-sm">
				{layout.popouts?.length ? 'Every open panel is in its own window.' : 'No panel is open.'}
			</p>
			<div class="flex gap-2">
				{#each closed as id (id)}
					<Button variant="outline" size="sm" onclick={() => open(id)}>
						<Plus />
						{titleOf(id)}
					</Button>
				{/each}
			</div>
		</div>
	{/if}
</div>

<style>
	.rask-dock-frame {
		position: relative;
		display: flex;
		flex: 1 1 0;
		min-height: 0;
		min-width: 0;
	}
	.rask-dock-panel {
		height: 100%;
		width: 100%;
		overflow: auto;
	}
	.rask-dock-maximized-bar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
		padding: 0.25rem 0.5rem;
		border-bottom: 1px solid var(--border);
		background: var(--background);
	}
	/* Over the empty layout, which stays mounted for its popout windows. */
	.rask-dock-empty {
		position: absolute;
		inset: 0;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 0.75rem;
	}
</style>
