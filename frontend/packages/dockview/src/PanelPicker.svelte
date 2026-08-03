<script lang="ts">
	/**
	 * The `+` add-panel picker — a searchable list of the zone's catalogue.
	 *
	 * SEPARATE from split, and the separation is the point rather than a detail. Split acts on the
	 * PANE — divide this space in a direction. `+` acts on the CONTENT — put a named panel here.
	 * Conflating them is what produced a split button whose only possible outcome was a second copy of
	 * whatever you were already looking at.
	 *
	 * Same native Popover API as `SplitMenu`, for the same three reasons — see that file's header for
	 * why `.dv-groupview`'s overflow, `.dv-animation`'s transform and `.dv-render-overlay`'s z-index
	 * defeat every ordinary positioning strategy here.
	 *
	 * Hand-rolled rather than built on Bits UI's Command: this package must not depend on `@rask/ui`
	 * (which has a `svelte-package` build step, and with it the `^build` ordering + `dist/` watcher
	 * race), and the deferred bundle headroom is single-digit KB across three zones.
	 */
	import { matchesPanel, type PanelChoice } from './panel-search';

	interface Props {
		/** DOM id — the trigger's `popovertarget`. Unique per document. */
		id: string;
		/** The trigger's DOM id, so the list can measure it without a shared element ref. */
		anchorId: string;
		/** The catalogue, already flattened and marked with what is open. */
		choices: PanelChoice[];
		/** Hides the list first, then reports the pick. */
		onpick: (key: string) => void;
		/** Mirrors the UA's open state so the trigger can carry an honest `aria-expanded`. */
		onopenchange?: (open: boolean) => void;
	}
	let { id, anchorId, choices, onpick, onopenchange }: Props = $props();

	let menu: HTMLDivElement | null = $state(null);
	let field: HTMLInputElement | null = $state(null);
	let query = $state('');

	const shown = $derived(choices.filter((c) => matchesPanel(c, query)));
	/** Grouped view of `shown`, insertion-ordered; headings render only when there is >1 section, so
	 *  a small ungrouped registry keeps its flat list. */
	const sections = $derived.by(() => {
		const bag = new Map<string, typeof shown>();
		for (const c of shown) {
			const g = c.group ?? 'Panels';
			const list = bag.get(g);
			if (list === undefined) bag.set(g, [c]);
			else list.push(c);
		}
		return [...bag.entries()];
	});

	/** Anchor under the trigger, flipping up near the bottom edge and clamping to the viewport. */
	function place(): void {
		if (menu === null) return;
		const doc = menu.ownerDocument;
		const view = doc.defaultView;
		const anchor = doc.getElementById(anchorId);
		if (view === null || anchor === null) return;
		const a = anchor.getBoundingClientRect();
		const m = menu.getBoundingClientRect();
		const left = Math.min(Math.max(4, a.right - m.width), view.innerWidth - m.width - 4);
		const below = a.bottom + 4;
		const top = below + m.height > view.innerHeight - 4 ? a.top - m.height - 4 : below;
		menu.style.left = `${Math.round(left)}px`;
		menu.style.top = `${Math.round(top)}px`;
	}

	function ontoggle(event: ToggleEvent): void {
		const open = event.newState === 'open';
		onopenchange?.(open);
		if (!open) {
			// Reset on CLOSE, not on open: a stale query would otherwise be visible for one frame while
			// the list re-filters, which reads as the picker forgetting what you typed a moment late.
			query = '';
			return;
		}
		place();
		// Focus the field, not the first row — the point of the search box is that you can start typing.
		field?.focus();
	}

	function pick(key: string): void {
		menu?.hidePopover();
		onpick(key);
	}

	/** Enter with exactly one match is the fast path: type three letters, press Enter, done. */
	function onkeydown(event: KeyboardEvent): void {
		const only = shown[0];
		if (event.key === 'Enter' && only !== undefined && shown.length === 1) {
			event.preventDefault();
			pick(only.key);
		}
	}
</script>

<div
	bind:this={menu}
	{id}
	popover="auto"
	class="rask-panel-picker"
	role="group"
	aria-label="Add a panel"
	{ontoggle}
>
	<input
		bind:this={field}
		bind:value={query}
		{onkeydown}
		type="search"
		class="field"
		placeholder="Search panels…"
		aria-label="Search panels"
		autocomplete="off"
	/>
	{#each sections as [heading, list] (heading)}
		{#if sections.length > 1}<p class="heading">{heading}</p>{/if}
		<ul class="list">
			{#each list as choice (choice.key)}
				<li>
					<button type="button" class="row" onclick={() => pick(choice.key)}>
						{#if choice.icon}
							{@const Icon = choice.icon}
							<Icon size={14} />
						{/if}
						<span class="label">{choice.label}</span>
						<!-- Already-open panels stay listed and stay clickable: a second Runs panel filtered
					     differently is a real thing to want, and `uniquePanelId` makes the copy safe. The
					     note just stops it looking like nothing happened. -->
						{#if choice.open}<span class="open">open</span>{/if}
					</button>
				</li>
			{/each}
			{#if shown.length === 0}
				<li class="empty">No panel matches “{query}”.</li>
			{/if}
		</ul>
	{/each}
</div>

<style>
	/* `display` MUST be scoped to `:popover-open`, and this is not a style preference.
	   The UA stylesheet hides a closed popover with `[popover]:not(:popover-open) { display: none }`,
	   but AUTHOR rules beat user-agent rules by origin — specificity never enters into it — so a plain
	   `.rask-panel-picker { display: flex }` silently un-hides every closed popover. Measured in
	   Chromium: `:popover-open` false, computed display `flex`, laid out at (1034, 44), intercepting
	   pointer events on its own trigger so the picker could never be opened. Nothing in the markup or
	   the unit tests can see this; only a browser can. */
	.rask-panel-picker:popover-open {
		display: flex;
	}
	.rask-panel-picker {
		/* Ten grouped rows outgrew the viewport on shallow docks — the popover now caps and its LIST
		   scrolls (the field stays pinned), instead of the whole menu spilling off-screen. */
		max-height: min(60vh, 480px);
		display: flex;
		flex-direction: column;

		position: fixed;
		inset: auto;
		margin: 0;
		flex-direction: column;
		gap: 4px;
		width: 220px;
		max-height: 320px;
		padding: 6px;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 4px);
		background: var(--popover);
		color: var(--popover-foreground);
		box-shadow: 0 10px 30px -10px oklch(0 0 0 / 45%);
	}
	.field {
		width: 100%;
		padding: 4px 6px;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 6px);
		background: var(--background);
		color: var(--foreground);
		font-size: 12px;
	}
	.field:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -1px;
	}
	.heading {
		margin: 6px 4px 2px;
		font-size: 10px;
		font-weight: 500;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--muted-foreground);
	}
	.list {
		overflow-y: auto;

		display: flex;
		flex-direction: column;
		gap: 1px;
		margin: 0;
		padding: 0;
		overflow-y: auto;
		list-style: none;
	}
	.row {
		display: flex;
		align-items: center;
		gap: 6px;
		width: 100%;
		padding: 5px 6px;
		border: none;
		border-radius: calc(var(--radius) - 6px);
		background: transparent;
		color: var(--foreground);
		font-size: 12px;
		text-align: start;
		cursor: pointer;
	}
	.row:hover {
		background: var(--accent);
	}
	.row:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}
	.label {
		flex: 1 1 auto;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.open {
		flex: 0 0 auto;
		color: var(--muted-foreground);
		font-size: 10px;
	}
	.empty {
		padding: 6px;
		color: var(--muted-foreground);
		font-size: 11px;
	}
</style>
