<script lang="ts">
	import { NavigationMenu as NavigationMenuPrimitive } from 'bits-ui';
	import { on } from 'svelte/events';
	import { cn } from '../../utils/index.js';
	import NavigationMenuViewport from './navigation-menu-viewport.svelte';

	// The shadcn-svelte NavigationMenu shape on bits-ui: a horizontal menubar whose items either
	// link straight out or open a floating panel. `viewport` (default true) renders the shared
	// animated panel container that every Content portals into — one box that resizes between
	// items, instead of a popover per trigger. Set `viewport={false}` for per-item popovers.
	let {
		ref = $bindable(null),
		class: className,
		viewport = true,
		children,
		...restProps
	}: NavigationMenuPrimitive.RootProps & { viewport?: boolean } = $props();

	// THE PANEL FOLLOWS ITS TRIGGER.
	//
	// Upstream pins the shared viewport at `left-0` of the ROOT, which is fine for a three-item demo
	// bar and wrong for this estate's. Measured on the deployed build at 1440px: the Lakehouse panel
	// opened at x=313 while its own trigger began at x=409 — 96px to the LEFT of it — and every other
	// trigger got that identical anchor, so Search (250px further right) opened a panel nowhere near
	// itself. Reaching for it means crossing dead space, which drops the hover and closes the menu
	// before the pointer lands. That is the "dropdown is off to the left and I can't press anything"
	// report, and it is a POSITIONING bug, not hover-intent: no delay tuning fixes a panel that is in
	// the wrong place.
	//
	// So: centre the viewport under whichever trigger is open, then clamp it into the window so a
	// right-hand trigger's panel cannot hang off the edge — the clamp is why this is not simply
	// `left: trigger.offsetLeft`. Measured from the DOM rather than mirrored in state because bits-ui
	// owns which item is open; the trigger's `data-state` is the primitive's own public signal, so a
	// MutationObserver on it stays correct through pointer, keyboard and programmatic opens alike.
	let panelLeft = $state(0);
	let panelTop = $state(0);

	$effect(() => {
		const root = ref;
		if (!root || !viewport) return;

		const place = (): void => {
			const open = root.querySelector<HTMLElement>('[data-state="open"][aria-expanded="true"]');
			const panel = root.querySelector<HTMLElement>('[data-slot="navigation-menu-viewport"]');
			if (!open || !panel) return;
			const panelWidth = panel.getBoundingClientRect().width;
			// A panel mid-open animation can measure 0. Keeping the last position beats snapping to the
			// far left for a frame, and the observer fires again once the real width lands.
			if (panelWidth === 0) return;
			const trigger = open.getBoundingClientRect();
			const centred = trigger.left + trigger.width / 2 - panelWidth / 2;
			const GUTTER = 8;
			const clamped = Math.min(
				Math.max(GUTTER, centred),
				Math.max(GUTTER, window.innerWidth - panelWidth - GUTTER),
			);
			// FIXED coordinates, not root-relative. The panel used to be `absolute` inside the nav,
			// which put it inside `MAIN`'s `overflow: hidden` — and MAIN starts where the SIDEBAR ends
			// (x=256). Any panel wide enough to extend left of that had its first column silently
			// CLIPPED: the Lakehouse panel measured x=9..745 and painted from 256, losing the whole
			// Catalog column. It reads exactly like "the sidebar is on top of the dropdown", which is
			// why raising z-index was the obvious fix and never worked — z-50 already beat the
			// sidebar's z-10; nothing was overlapping, the paint was clipped.
			panelLeft = clamped;
			panelTop = root.getBoundingClientRect().bottom;
		};

		// `data-state` flips on the trigger and the panel's width lands a frame later, so re-measure on
		// both. childList too, because the viewport's contents are swapped when you move from one open
		// trigger straight to another without closing.
		const observer = new MutationObserver(() => requestAnimationFrame(place));
		observer.observe(root, {
			subtree: true,
			childList: true,
			attributes: true,
			attributeFilter: ['data-state', 'style'],
		});
		// `on` from svelte/events, not addEventListener: it returns its own unsubscriber and is what the
		// estate's lint rule requires (a hand-rolled removeEventListener drifts from the handler it pairs
		// with the first time someone edits one of the two lines).
		const offResize = on(window, 'resize', place);
		requestAnimationFrame(place);
		return () => {
			observer.disconnect();
			offResize();
		};
	});
</script>

<NavigationMenuPrimitive.Root
	bind:ref
	data-slot="navigation-menu"
	data-viewport={viewport}
	class={cn(
	'group/navigation-menu relative flex max-w-max flex-1 items-center justify-center',
	className,
)}
	{...restProps}
>
	{@render children?.()}
	{#if viewport}
		<NavigationMenuViewport left={panelLeft} top={panelTop} />
	{/if}
</NavigationMenuPrimitive.Root>
