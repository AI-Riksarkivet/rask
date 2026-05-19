<script lang="ts">
	import { Loader2, CheckCircle, XCircle } from 'lucide-svelte';
	import { Badge } from '$lib/components/ui/badge/index.js';

	let {
		steps,
		class: className = '',
	}: {
		steps: {
			label: string;
			group?: string;
			status: 'pending' | 'running' | 'done' | 'failed';
			error?: string;
		}[];
		class?: string;
	} = $props();
</script>

<div class="max-h-48 space-y-1 overflow-y-auto rounded-md border p-3 {className}">
	{#each steps as step (step.group ? `${step.group}:${step.label}` : step.label)}
		<div class="flex items-center gap-2 text-sm">
			{#if step.status === 'pending'}
				<div class="h-4 w-4 rounded-full border border-muted-foreground"></div>
			{:else if step.status === 'running'}
				<Loader2 class="h-4 w-4 animate-spin text-primary" />
			{:else if step.status === 'done'}
				<CheckCircle class="h-4 w-4 text-green-600" />
			{:else}
				<XCircle class="h-4 w-4 text-destructive" />
			{/if}
			<span class="truncate">
				{#if step.group}
					<span class="font-medium">{step.group}</span>: {step.label}
				{:else}
					{step.label}
				{/if}
			</span>
			{#if step.error}
				<Badge variant="destructive" class="ml-auto shrink-0 text-xs">
					{step.error}
				</Badge>
			{/if}
		</div>
	{/each}
</div>
