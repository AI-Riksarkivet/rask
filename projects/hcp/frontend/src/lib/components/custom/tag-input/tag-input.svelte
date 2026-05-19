<script lang="ts">
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { X } from 'lucide-svelte';

	let {
		tags = $bindable<string[]>([]),
		placeholder = 'Add tag...',
		disabled = false,
	}: {
		tags: string[];
		placeholder?: string;
		disabled?: boolean;
	} = $props();

	let input = $state('');

	function add() {
		const t = input.trim().toLowerCase();
		if (t && !tags.includes(t)) {
			tags = [...tags, t];
		}
		input = '';
	}

	function remove(tag: string) {
		tags = tags.filter((t) => t !== tag);
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter') {
			e.preventDefault();
			add();
		}
	}
</script>

<div class="space-y-2">
	<div class="flex gap-2">
		<Input {placeholder} bind:value={input} onkeydown={handleKeydown} {disabled} />
		<Button variant="secondary" size="sm" onclick={add} {disabled}>Add</Button>
	</div>
	{#if tags.length > 0}
		<div class="flex flex-wrap gap-1.5">
			{#each tags as tag (tag)}
				<Badge variant="secondary" class="gap-1 pr-1">
					{tag}
					{#if !disabled}
						<button
							type="button"
							class="rounded-full p-0.5 text-muted-foreground hover:text-destructive"
							onclick={() => remove(tag)}
						>
							<X class="h-2.5 w-2.5" />
						</button>
					{/if}
				</Badge>
			{/each}
		</div>
	{/if}
</div>
