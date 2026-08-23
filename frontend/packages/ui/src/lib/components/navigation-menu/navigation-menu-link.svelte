<script lang="ts">
	import { NavigationMenu as NavigationMenuPrimitive } from 'bits-ui';
	import { cn } from '../../utils/index.js';

	let {
		ref = $bindable(null),
		class: className,
		...restProps
	}: NavigationMenuPrimitive.LinkProps = $props();
</script>

<NavigationMenuPrimitive.Link
	bind:ref
	data-slot="navigation-menu-link"
	class={cn(
		// bits-ui v2 sets a BARE `data-active` on the active link (not `data-active="true"`, which
		// is what upstream shadcn's selectors target) — so match on attribute presence, exactly as
		// the shell navbar already does for its own entries.
		"data-[active]:bg-muted data-[active]:text-foreground data-[active]:hover:bg-muted data-[active]:focus:bg-muted hover:bg-muted hover:text-foreground focus:bg-muted focus:text-foreground focus-visible:border-ring focus-visible:ring-ring/50 [&_svg:not([class*='text-'])]:text-muted-foreground flex flex-col gap-1 rounded-md border border-transparent p-2 text-sm transition-colors outline-none focus-visible:ring-3 [&_svg:not([class*='size-'])]:size-4",
		className,
	)}
	{...restProps}
/>
