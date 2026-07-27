<script lang="ts">
	import { cn, type WithElementRef } from '../../utils/index.js';
	import { Skeleton } from '../skeleton/index.js';
	import type { HTMLAttributes } from 'svelte/elements';

	let {
		ref = $bindable(null),
		class: className,
		showIcon = false,
		width = '70%',
		children,
		...restProps
	}: WithElementRef<HTMLAttributes<HTMLElement>> & {
		showIcon?: boolean;
		/** Text-row width. Fixed by default so SSR HTML matches the hydration frame
		 * (a random per-render width would mismatch). Callers can vary it deterministically. */
		width?: string;
	} = $props();
</script>

<div
	bind:this={ref}
	data-slot="sidebar-menu-skeleton"
	data-sidebar="menu-skeleton"
	class={cn('flex h-8 items-center gap-2 rounded-md px-2', className)}
	{...restProps}
>
	{#if showIcon}
		<Skeleton class="size-4 rounded-md" data-sidebar="menu-skeleton-icon" />
	{/if}
	<Skeleton
		class="h-4 max-w-(--skeleton-width) flex-1"
		data-sidebar="menu-skeleton-text"
		--skeleton-width={width}
	/>
	{@render children?.()}
</div>
