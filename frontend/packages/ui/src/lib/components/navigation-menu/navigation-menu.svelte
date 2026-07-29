<script lang="ts">
	import { NavigationMenu as NavigationMenuPrimitive } from 'bits-ui';
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
		<NavigationMenuViewport />
	{/if}
</NavigationMenuPrimitive.Root>
