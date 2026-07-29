<script lang="ts">
	/**
	 * The split direction pad — a compass of four, opened from ONE header button.
	 *
	 * WHY THE NATIVE POPOVER API, and not an absolutely-positioned div. Three separate ancestor rules
	 * in dockview's own stylesheet make ordinary positioning fail here, each of them silently:
	 *
	 *   * `.dv-groupview { overflow: hidden }` (dockview.css:2496-2502) clips any `position: absolute`
	 *     child to the group rectangle. The header strip is 36px tall, so a 90px pad is simply cut off.
	 *   * `.dv-split-view-container.dv-animation .dv-view { will-change: transform }` (:2878-2884) makes
	 *     the grid cell a containing block for FIXED descendants while an add/remove/resize animation
	 *     runs — so `position: fixed` is not an escape either; it snaps back inside and re-clips.
	 *   * `.dv-render-overlay { z-index: 1 }` (:2741-2753): under `defaultRenderer: 'always'` panel
	 *     BODIES are appended to the shell as later siblings of the gridview, so anything in a group
	 *     header paints beneath them unless it carries `z-index >= 2`.
	 *
	 * An open `popover` renders in the TOP LAYER, whose containing block is the viewport. It is not
	 * clipped by an ancestor's overflow, not captured by an ancestor's transform, and always above the
	 * page — the only mechanism that answers all three without a line of z-index arithmetic.
	 *
	 * It also buys the dismissal behaviour for free: `popover="auto"` gives UA light-dismiss on
	 * outside-pointerdown and on Escape, and returns focus to the invoker. That is the whole
	 * "dismiss without a focus-trap library" requirement, at zero bytes. `popovertarget` on the trigger
	 * rather than a hand-rolled `onclick` toggle matters too — a click on an open popover's invoker
	 * fires light-dismiss AND the handler, so a hand-rolled toggle would reopen it and never close.
	 */
	import { SPLIT_DIRECTIONS, type SplitPosition } from './split';

	interface Props {
		/** DOM id — the trigger's `popovertarget`. Must be unique per document. */
		id: string;
		/** The trigger's DOM id, so the pad can measure it without a shared element ref. */
		anchorId: string;
		/** What the action will actually do. Pass `splitVerb(count)`, never a fixed string. */
		verb: 'Split' | 'Duplicate';
		/** Hides the pad first, then reports the pick — the split re-lays out the grid underneath. */
		onpick: (position: SplitPosition) => void;
		/** Mirrors the UA's open state so the trigger can carry an honest `aria-expanded`. */
		onopenchange?: (open: boolean) => void;
	}
	let { id, anchorId, verb, onpick, onopenchange }: Props = $props();

	let menu: HTMLDivElement | null = $state(null);

	/**
	 * Anchor the top-layer box under the trigger, flipping above it near the bottom edge and clamping
	 * to the viewport.
	 *
	 * Hand-rolled rather than CSS anchor positioning (`anchor-name` / `position-area`): Firefox support
	 * is not safe to assume across this estate, and this is ~12 lines that work everywhere.
	 *
	 * `ownerDocument` / `defaultView`, never the `document` and `window` globals: a popped-out group
	 * lives in a SECOND browser window with its own document. Split is gated off in popout today
	 * (`GroupActions.svelte`'s `canSplit` requires a grid location), but measuring against the wrong
	 * viewport is precisely the bug that would appear the day that gate relaxes, and it costs nothing
	 * to be right now.
	 */
	function place(): void {
		if (menu === null) return;
		const doc = menu.ownerDocument;
		const view = doc.defaultView;
		const anchor = doc.getElementById(anchorId);
		if (view === null || anchor === null) return;
		const a = anchor.getBoundingClientRect();
		// Measurable because `:popover-open` is already laid out by the time `toggle` fires.
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
		if (!open || menu === null) return;
		place();
		// The UA does not move focus into a popover, so a keyboard user would otherwise open the pad
		// and still be standing on the trigger.
		menu.querySelector('button')?.focus();
	}

	function pick(position: SplitPosition): void {
		// Hide BEFORE splitting: the split re-lays out the grid, and a pad still anchored to a button
		// that has just moved reads as a glitch.
		menu?.hidePopover();
		onpick(position);
	}
</script>

<div
	bind:this={menu}
	{id}
	popover="auto"
	class="rask-split-menu"
	role="group"
	aria-label="Split direction"
	{ontoggle}
>
	{#each SPLIT_DIRECTIONS as d (d.position)}
		<button
			type="button"
			class="cell {d.cell}"
			title={`${verb} ${d.word}`}
			aria-label={`${verb} ${d.word}`}
			onclick={() => pick(d.position)}
		>
			<!-- ONE arrow path, rotated per direction — the same trick dockview uses for its own
			     chevrons, and three fewer icon imports than four lucide components. -->
			<svg
				viewBox="0 0 24 24"
				width="13"
				height="13"
				aria-hidden="true"
				style:rotate="{d.rotate}deg"
			>
				<path
					d="M12 19V5M5 12l7-7 7 7"
					fill="none"
					stroke="currentColor"
					stroke-width="2"
					stroke-linecap="round"
					stroke-linejoin="round"
				/>
			</svg>
		</button>
	{/each}
	<!-- The live verb sits in the middle cell, so the pad is honest about the one-panel case (where
	     split duplicates rather than moves) without needing a fifth row to explain itself. -->
	<span class="verb">{verb}</span>
</div>

<style>
	.rask-split-menu {
		/* Overrides the UA's default `inset: 0; margin: auto`, which would centre the pad in the
		   viewport. `place()` then writes left/top. */
		position: fixed;
		inset: auto;
		margin: 0;
		display: grid;
		grid-template-areas: '. n .' 'w v e' '. s .';
		gap: 2px;
		padding: 4px;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 4px);
		background: var(--popover);
		color: var(--popover-foreground);
		box-shadow: 0 10px 30px -10px oklch(0 0 0 / 45%);
	}
	.cell {
		display: grid;
		place-items: center;
		width: 26px;
		height: 26px;
		border: none;
		border-radius: calc(var(--radius) - 6px);
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
	}
	.cell:hover {
		color: var(--foreground);
		background: var(--accent);
	}
	/* Not optional: these four are the only way to reach up/left from the keyboard. */
	.cell:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}
	.n {
		grid-area: n;
	}
	.w {
		grid-area: w;
	}
	.e {
		grid-area: e;
	}
	.s {
		grid-area: s;
	}
	.verb {
		grid-area: v;
		place-self: center;
		font-size: 9px;
		line-height: 1;
		color: var(--muted-foreground);
	}
</style>
