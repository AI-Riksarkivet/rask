<script lang="ts">
	import { ArrowLeft } from 'lucide-svelte';
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';

	let {
		href,
		label,
	}: {
		href: string;
		label: string;
	} = $props();
</script>

<Tooltip.Root>
	<Tooltip.Trigger>
		{#snippet child({ props })}
			<a
				{href}
				class="rounded-lg p-2 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
				{...props}
			>
				<ArrowLeft class="h-5 w-5" />
			</a>
		{/snippet}
	</Tooltip.Trigger>
	<Tooltip.Content>{label}</Tooltip.Content>
</Tooltip.Root>
