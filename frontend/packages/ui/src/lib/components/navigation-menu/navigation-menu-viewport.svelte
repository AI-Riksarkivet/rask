<script lang="ts">
	import { NavigationMenu as NavigationMenuPrimitive } from 'bits-ui';
	import { cn } from '../../utils/index.js';

	let {
		ref = $bindable(null),
		class: className,
		left = 0,
		...restProps
	}: NavigationMenuPrimitive.ViewportProps & {
		/** Offset from the menu ROOT's left edge, in px — the parent centres this under whichever
		 *  trigger is open and clamps it into the window. Defaults to 0, which IS the upstream
		 *  behaviour (pinned to the bar's left edge) and stays correct for a bar narrow enough that
		 *  every trigger sits near that edge. */
		left?: number;
	} = $props();
</script>

<!-- `left` is an inline style rather than a class because it is a MEASURED value: Tailwind cannot
     express "centred under the third of eight triggers, clamped to the window". SSR renders it at 0
     (there is no DOM to measure yet) and the parent's effect places it on the first frame the panel
     actually opens, which is also the first frame anyone can see it. -->
<div class="absolute top-full isolate z-50 flex justify-center" style="left: {left}px">
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
