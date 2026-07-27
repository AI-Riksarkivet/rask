<script lang="ts">
	import { cn, type WithElementRef } from '../../utils/index.js';
	import type { HTMLAttributes } from 'svelte/elements';

	let {
		ref = $bindable(null),
		class: className,
		children,
		...restProps
	}: WithElementRef<HTMLAttributes<HTMLElement>> = $props();
</script>

<main
	bind:this={ref}
	data-slot="sidebar-inset"
	class={cn(
	// `min-w-0`: the inset is a flex CHILD of the sidebar wrapper, so without it its automatic
	// minimum size is its content's min-content width — one wide table and the whole shell (and
	// with it the document) grows past the viewport and the page slices off the right edge.
	// With it, wide content is bounded by the inset and has to own its own scroll container.
	'bg-background relative flex w-full min-w-0 flex-1 flex-col md:peer-data-[variant=inset]:m-2 md:peer-data-[variant=inset]:ml-0 md:peer-data-[variant=inset]:rounded-xl md:peer-data-[variant=inset]:shadow-sm md:peer-data-[variant=inset]:peer-data-[state=collapsed]:ml-2',
	className,
)}
	{...restProps}
>
	{@render children?.()}
</main>
