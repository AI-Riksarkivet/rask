<script lang="ts">
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';

	let {
		namespaces = $bindable([]),
	}: {
		namespaces: string[];
	} = $props();

	let nsInput = $state('');

	function addNamespace() {
		const ns = nsInput.trim();
		if (ns && !namespaces.includes(ns)) {
			namespaces = [...namespaces, ns];
		}
		nsInput = '';
	}

	function removeNamespace(ns: string) {
		namespaces = namespaces.filter((n) => n !== ns);
	}

	function handleNsKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter') {
			e.preventDefault();
			addNamespace();
		}
	}
</script>

<div class="rounded-lg border p-5">
	<h3 class="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
		Assigned Namespaces
	</h3>
	<p class="mt-1 text-xs text-muted-foreground">
		Namespaces where this content class is active for metadata indexing.
	</p>
	<div class="mt-3">
		{#if namespaces.length > 0}
			<div class="mb-3 flex flex-wrap gap-1.5">
				{#each namespaces as ns (ns)}
					<Badge variant="secondary" class="gap-1">
						{ns}
						<button
							class="ml-0.5 rounded-full p-0.5 hover:bg-muted"
							onclick={() => removeNamespace(ns)}
						>
							<span class="sr-only">Remove {ns}</span>
							&times;
						</button>
					</Badge>
				{/each}
			</div>
		{/if}
		<div class="flex gap-2">
			<Input
				class="h-8 max-w-xs text-sm"
				placeholder="Namespace name"
				bind:value={nsInput}
				onkeydown={handleNsKeydown}
			/>
			<Button variant="outline" size="sm" onclick={addNamespace}>Add</Button>
		</div>
	</div>
</div>
