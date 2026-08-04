<script lang="ts">
	import { NavigationMenu as NavigationMenuPrimitive } from 'bits-ui';
	import { cn } from '../../utils/index.js';

	let {
		ref = $bindable(null),
		class: className,
		left = 0,
		top = 0,
		...restProps
	}: NavigationMenuPrimitive.ViewportProps & {
		/** VIEWPORT coordinates, in px — the parent centres the panel under whichever trigger is open
		 *  and clamps it into the window. */
		left?: number;
		top?: number;
	} = $props();
</script>

<!-- FIXED, not absolute. As an absolute child of the nav this sat inside `MAIN`'s `overflow: hidden`,
     and MAIN begins where the sidebar ends — so any panel reaching left of that had its first column
     clipped away and the sidebar appeared to be painted on top of it. Fixed positioning leaves every
     clipping ancestor behind; z-50 then genuinely wins because nothing above it creates a stacking
     context.

     Both coordinates are inline styles because they are MEASURED: Tailwind cannot express "centred
     under the third of eight triggers, clamped to the window". SSR renders 0/0 with the panel closed
     (nothing to measure, nothing visible); the parent places it on the first frame it opens. -->
<div class="fixed isolate z-50 flex justify-center" style="left: {left}px; top: {top}px">
	<NavigationMenuPrimitive.Viewport
		bind:ref
		data-slot="navigation-menu-viewport"
		class={cn(
	'origin-top-center bg-popover text-popover-foreground data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-90 relative mt-1.5 h-[var(--bits-navigation-menu-viewport-height)] w-full overflow-hidden rounded-lg border shadow-md md:w-[var(--bits-navigation-menu-viewport-width)]',
	className,
)}
		{...restProps}
	/>
</div>
