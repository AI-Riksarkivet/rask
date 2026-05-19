<script lang="ts">
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Plus, X } from 'lucide-svelte';

	let {
		addresses = $bindable<string[]>([]),
		label = '',
		placeholder = 'IP address or CIDR (e.g. 10.0.0.0/8)',
		variant = 'secondary' as 'secondary' | 'destructive',
		emptyText = 'No addresses configured.',
		disabled = false,
	}: {
		addresses: string[];
		label?: string;
		placeholder?: string;
		variant?: 'secondary' | 'destructive';
		emptyText?: string;
		disabled?: boolean;
	} = $props();

	let input = $state('');

	function add() {
		const val = input.trim();
		if (val && !addresses.includes(val)) {
			addresses = [...addresses, val];
		}
		input = '';
	}

	function remove(index: number) {
		addresses = addresses.filter((_, i) => i !== index);
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter') {
			e.preventDefault();
			add();
		}
	}
</script>

<div class="space-y-2">
	{#if label}
		<Label>{label}</Label>
	{/if}
	<div class="flex gap-2">
		<Input {placeholder} bind:value={input} onkeydown={handleKeydown} {disabled} />
		<Button variant="outline" size="icon" onclick={add} {disabled}>
			<Plus class="h-4 w-4" />
		</Button>
	</div>
	{#if addresses.length > 0}
		<div class="flex flex-wrap gap-1.5">
			{#each addresses as addr, i (addr)}
				<Badge {variant} class="gap-1 pr-1">
					{addr}
					{#if !disabled}
						<button
							class="rounded-full p-0.5 hover:bg-muted-foreground/20"
							onclick={() => remove(i)}
						>
							<X class="h-3 w-3" />
						</button>
					{/if}
				</Badge>
			{/each}
		</div>
	{:else}
		<p class="text-xs text-muted-foreground">{emptyText}</p>
	{/if}
</div>
