<script lang="ts" module>
	import { cn } from '../../utils/index.js';
	import { tv } from 'tailwind-variants';

	/** The shared entry-chrome for the menubar row. Exported so a PLAIN link (a zone with no
	 *  sub-areas, which needs no trigger) sits at exactly the same height, padding and radius as
	 *  a trigger — the row cannot drift between the two kinds of entry. House tokens, not
	 *  shadcn's raw defaults: `h-8`/`rounded-lg`/`px-2.5`/`ring-3` mirror the Button base. */
	export const navigationMenuTriggerStyle = tv({
		base: 'hover:bg-muted hover:text-foreground focus-visible:bg-muted focus-visible:text-foreground data-[state=open]:bg-muted data-[state=open]:text-foreground data-[active]:bg-muted data-[active]:text-foreground focus-visible:border-ring focus-visible:ring-ring/50 text-muted-foreground group inline-flex h-8 w-max flex-row items-center justify-center gap-1 rounded-lg border border-transparent px-2.5 text-sm font-medium whitespace-nowrap transition-colors outline-none focus-visible:ring-3 disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0',
	});
</script>

<script lang="ts">
	import { NavigationMenu as NavigationMenuPrimitive } from 'bits-ui';
	import { ChevronDown } from '@lucide/svelte';

	let {
		ref = $bindable(null),
		class: className,
		children,
		...restProps
	}: NavigationMenuPrimitive.TriggerProps = $props();
</script>

<NavigationMenuPrimitive.Trigger
	bind:ref
	data-slot="navigation-menu-trigger"
	class={cn(navigationMenuTriggerStyle(), className)}
	{...restProps}
>
	{@render children?.()}
	<ChevronDown
		class="relative top-px size-3 transition-transform duration-200 group-data-[state=open]:rotate-180"
		aria-hidden="true"
	/>
</NavigationMenuPrimitive.Trigger>
