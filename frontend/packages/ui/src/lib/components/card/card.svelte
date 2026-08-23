<script lang="ts">
	import type { HTMLAttributes } from 'svelte/elements';
	import { cn } from '../../utils/cn.js';

	// The shared surface. `min-w-0` + `max-w-full`: a card is almost always a flex/grid child, whose
	// automatic minimum size is its CONTENT's min-content width — so one wide child (a table, a long
	// unbroken id, a code block) silently widens the card past its column and slices it off the
	// right edge of the viewport. Bounded here, wide content has to own its own scroll container
	// instead of pushing the document sideways.
	let { class: className, children, ...rest }: HTMLAttributes<HTMLDivElement> = $props();
</script>

<div
	data-slot="card"
	class={cn(
		'border-border bg-card text-card-foreground max-w-full min-w-0 rounded-lg border shadow-sm',
		className,
	)}
	{...rest}
>
	{@render children?.()}
</div>
