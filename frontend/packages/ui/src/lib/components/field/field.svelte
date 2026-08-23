<script lang="ts">
	import { cn } from '../../utils/index.js';
	import type { Snippet } from 'svelte';

	/**
	 * Form-group wrapper: a stacked label · control · description with one
	 * consistent rhythm. The `inline` variant lays the label and control out on
	 * one row (label left, control right) for compact settings rows.
	 */
	let {
		label,
		description,
		inline = false,
		class: className,
		children,
	}: {
		label?: string;
		description?: string;
		inline?: boolean;
		class?: string;
		children: Snippet;
	} = $props();
</script>

<div
	class={cn(
		inline ? 'flex items-center justify-between gap-3' : 'flex flex-col gap-1.5',
		className,
	)}
>
	{#if label}
		<span class="text-foreground text-xs font-medium">{label}</span>
	{/if}
	<div class={inline ? 'flex items-center gap-2' : 'contents'}>
		{@render children()}
	</div>
	{#if description && !inline}
		<span class="text-muted-foreground text-[11px]">{description}</span>
	{/if}
</div>
