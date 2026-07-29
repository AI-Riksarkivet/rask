<script lang="ts">
	/**
	 * The control cluster in every group's tab strip — the chrome that did not exist.
	 *
	 * dockview ships NO default header buttons; the per-tab close ✕ is the entire stock control set.
	 * These mount into the `right` header slot (`.dv-right-actions-container`), which is the classic
	 * VS Code position and the only place persistent group controls can live.
	 *
	 * Everything reactive is derived from dockview events rather than polled: `onDidLocationChange`
	 * flips float/popout to "dock back", and `onDidMaximizedGroupChange` flips maximize to restore.
	 * `IGroupHeaderProps` hands over three fields and nothing more — core builds no reactive props
	 * object for a framework, so the binding builds its own.
	 */
	import { onMount, untrack } from 'svelte';
	import {
		Copy,
		Maximize2,
		Minimize2,
		PictureInPicture2,
		SquareSplitHorizontal,
		X,
	} from '@lucide/svelte';
	import type { DockviewApi, DockviewGroupPanelApi, IDockviewGroupPanel } from 'dockview';
	import type { DockChrome, DockChromeOptions } from './chrome';
	import SplitMenu from './SplitMenu.svelte';
	import { splitPanel, splitVerb, type SplitPosition } from './split';

	interface Props {
		api: DockviewGroupPanelApi;
		containerApi: DockviewApi;
		group: IDockviewGroupPanel;
		chrome: DockChrome;
		options: DockChromeOptions;
	}
	let { api, containerApi, group, chrome, options }: Props = $props();

	/**
	 * The CONCRETE group, looked up by id.
	 *
	 * `IGroupHeaderProps.group` is the `IDockviewGroupPanel` *interface*, but `addFloatingGroup`,
	 * `addPopoutGroup` and `moveTo` all want the concrete `DockviewGroupPanel` class. `api.groups`
	 * returns the concrete type, so one lookup satisfies all three without a cast — and it is a
	 * getter, so it stays correct if the group is replaced underneath us.
	 */
	const self = $derived(containerApi.groups.find((g) => g.id === group.id));

	/** How many panels this group holds. Decides whether Split moves or duplicates — see `verb`. */
	let panelCount = $state(untrack(() => group.panels.length));

	/** Where this group currently lives. Drives the float/popout buttons' meaning.
	 *  Read ONCE at creation (the same `untrack` shape `@rask/ui`'s ResizableSplit uses for `initial`):
	 *  every later value arrives through `onDidLocationChange` below, not by re-reading the prop. */
	let location = $state(untrack(() => api.location));
	let maximized = $state(false);

	onMount(() => {
		// dockview's own events, not a timer — the group can be moved by a drag we did not initiate.
		const loc = api.onDidLocationChange(() => (location = api.location));
		const max = containerApi.onDidMaximizedGroupChange(() => (maximized = api.isMaximized()));
		// The group's own membership changes when a tab is dragged in or out, which is what decides
		// whether Split can do anything. Both events fire for THIS group only.
		const added = containerApi.onDidAddPanel(() => (panelCount = group.panels.length));
		const removed = containerApi.onDidRemovePanel(() => (panelCount = group.panels.length));
		maximized = api.isMaximized();
		return () => {
			loc.dispose();
			max.dispose();
			added.dispose();
			removed.dispose();
		};
	});

	const kind = $derived(typeof location === 'string' ? location : location.type);
	const inGrid = $derived(kind === 'grid');
	/** Split needs a grid to split within, so floating and popped-out groups are excluded. */
	const canSplit = $derived(chrome.split && inGrid);
	/**
	 * The verb the control shows. With one panel Split DUPLICATES rather than moves (see `split.ts`),
	 * so the title says so — a button that silently does something other than its label is worse than
	 * one that is honest about it. Reads `panelCount`, so it re-derives when tabs move in or out.
	 */
	const verb = $derived(splitVerb(panelCount));

	/**
	 * Popover wiring ids, derived from `group.id` rather than Svelte's `$props.id()`.
	 *
	 * Each group's header is its own `mount()` root (`header-actions.svelte.ts`), and the `$props.id()`
	 * counter restarts per root — so every group would mint the SAME id and every trigger would open
	 * the first group's pad. `group.id` is dockview-generated and unique across the dock.
	 */
	const menuId = $derived(`rask-split-menu-${group.id}`);
	const triggerId = $derived(`rask-split-trigger-${group.id}`);
	/** Mirrors the popover's UA-owned open state, so `aria-expanded` is not a guess. */
	let splitOpen = $state(false);

	/** One implementation, shared with the context menu — see `split.ts` for why it has two modes. */
	function split(position: SplitPosition): void {
		const panel = group.activePanel;
		if (self !== undefined && panel !== undefined && panel !== null) {
			splitPanel(containerApi, self, panel, position);
		}
	}

	function toggleMaximize(): void {
		if (api.isMaximized()) api.exitMaximized();
		else api.maximize();
	}

	function toggleFloat(): void {
		if (kind === 'floating') api.moveTo({ position: 'center' });
		else if (self !== undefined) containerApi.addFloatingGroup(self);
	}

	async function togglePopout(): Promise<void> {
		if (kind === 'popout') {
			api.moveTo({ position: 'center' });
			return;
		}
		// Returns false when the browser blocked the window — surfaced rather than swallowed, since a
		// popup blocker is the single most likely reason this does nothing.
		if (self === undefined) return;
		const opened = await containerApi.addPopoutGroup(self, {
			popoutUrl: options.popoutUrl,
		});
		if (!opened) popoutFailed = true;
	}

	let popoutFailed = $state(false);
	/** A zone that serves no blank document cannot pop out; say so rather than fail silently. */
	const canPopout = $derived(chrome.popout && options.popoutUrl !== undefined);
</script>

<div class="rask-dock-actions">
	{#if canSplit}
		<!-- ONE trigger where there were two direction buttons. The strip gets SMALLER (six controls to
		     five) while going from two reachable directions to four: up and left previously existed only
		     as tab-context-menu rows, which is an interaction nobody discovers. -->
		<button
			type="button"
			id={triggerId}
			popovertarget={menuId}
			aria-haspopup="menu"
			aria-expanded={splitOpen}
			title={`${verb} this pane…`}
			aria-label="Split this pane"
		>
			<SquareSplitHorizontal size={14} />
		</button>
	{/if}

	{#if chrome.float && kind !== 'popout'}
		<button
			type="button"
			title={kind === 'floating' ? 'Dock back into the layout' : 'Float above the layout'}
			aria-label={kind === 'floating' ? 'Dock back into the layout' : 'Float above the layout'}
			class:on={kind === 'floating'}
			onclick={toggleFloat}
		>
			<Copy size={14} />
		</button>
	{/if}

	{#if canPopout}
		<button
			type="button"
			title={kind === 'popout' ? 'Dock back into the layout' : 'Open in a new window'}
			aria-label={kind === 'popout' ? 'Dock back into the layout' : 'Open in a new window'}
			class:on={kind === 'popout'}
			class:failed={popoutFailed}
			onclick={togglePopout}
		>
			<PictureInPicture2 size={14} />
		</button>
	{/if}

	{#if chrome.maximize && inGrid}
		<button
			type="button"
			title={maximized ? 'Restore' : 'Maximize this group'}
			aria-label={maximized ? 'Restore' : 'Maximize this group'}
			class:on={maximized}
			onclick={toggleMaximize}
		>
			{#if maximized}<Minimize2 size={14} />{:else}<Maximize2 size={14} />{/if}
		</button>
	{/if}

	<button
		type="button"
		title="Close this group"
		aria-label="Close this group"
		onclick={() => api.close()}
	>
		<X size={14} />
	</button>
</div>

{#if canSplit}
	<!-- Rendered OUTSIDE `.rask-dock-actions`: it is a top-layer popover, so its position in the DOM is
	     irrelevant to where it paints, and keeping it out of the flex row means the closed pad
	     (`display: none`) can never influence the strip's width. -->
	<SplitMenu
		id={menuId}
		anchorId={triggerId}
		{verb}
		onpick={split}
		onopenchange={(open) => (splitOpen = open)}
	/>
{/if}

{#if popoutFailed}
	<span class="rask-dock-actions-note" role="status">Your browser blocked the popout window.</span>
{/if}

<style>
	/* Sized to the tab strip (--dv-tabs-and-actions-container-height), so the header does not grow
	   when the cluster appears. Colours come from the estate tokens, never dockview's own. */
	.rask-dock-actions {
		display: flex;
		align-items: center;
		gap: 1px;
		height: 100%;
		padding-inline: 4px;
	}
	button {
		display: grid;
		place-items: center;
		width: 22px;
		height: 22px;
		border: none;
		border-radius: calc(var(--radius) - 6px);
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
		transition:
			color 0.12s ease,
			background 0.12s ease;
	}
	button:hover {
		color: var(--foreground);
		background: var(--accent);
	}
	/* The focus ring is not optional: these are the group's only keyboard-reachable controls. */
	button:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}
	button.on {
		color: var(--primary);
	}
	button.failed {
		color: var(--destructive);
	}
	/* Visibly present but inert, rather than vanishing: a control that appears and disappears as tabs
	   move is harder to learn than one that greys out and explains itself in its title. */
	button:disabled {
		opacity: 0.35;
		cursor: default;
	}
	button:disabled:hover {
		color: var(--muted-foreground);
		background: transparent;
	}
	.rask-dock-actions-note {
		position: absolute;
		right: 8px;
		top: 100%;
		z-index: 5;
		padding: 4px 8px;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 4px);
		background: var(--popover);
		color: var(--destructive);
		font-size: 11px;
		white-space: nowrap;
	}
</style>
