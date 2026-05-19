<script lang="ts">
	import { Plus, Trash2, HelpCircle } from 'lucide-svelte';
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Checkbox } from '$lib/components/ui/checkbox/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import type { ContentProperty } from '$lib/remote/content-classes.remote.js';

	const PROPERTY_TYPES = ['STRING', 'INTEGER', 'DOUBLE', 'BOOLEAN', 'DATETIME'] as const;

	let {
		properties = $bindable([]),
	}: {
		properties: ContentProperty[];
	} = $props();

	function addProperty() {
		properties = [...properties, { name: '', expression: '', type: 'STRING', multivalued: false }];
	}

	function removeProperty(index: number) {
		properties = properties.filter((_, i) => i !== index);
	}
</script>

<div class="rounded-lg border p-5">
	<div class="flex items-center justify-between">
		<h3 class="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
			Content Properties
		</h3>
		<Button variant="outline" size="sm" onclick={addProperty}>
			<Plus class="h-3.5 w-3.5" /> Add Property
		</Button>
	</div>

	{#if properties.length > 0}
		<div class="mt-3 space-y-2">
			{#each properties as prop, i (i)}
				<div class="flex items-end gap-2 rounded-md border p-3">
					<div class="min-w-0 flex-1">
						<div class="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
							<div class="space-y-1">
								<Label class="text-xs">Name</Label>
								<Input
									class="h-8 text-sm"
									placeholder="field_name"
									bind:value={properties[i].name}
								/>
							</div>
							<div class="space-y-1">
								<Label class="text-xs">Expression</Label>
								<Input
									class="h-8 text-sm"
									placeholder="//xpath or $.jsonpath"
									bind:value={properties[i].expression}
								/>
							</div>
							<div class="space-y-1">
								<Label class="text-xs">Type</Label>
								<select
									class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-8 w-full items-center rounded-md border px-3 py-1 text-sm shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
									bind:value={properties[i].type}
								>
									{#each PROPERTY_TYPES as t (t)}
										<option value={t}>{t}</option>
									{/each}
								</select>
							</div>
							<div class="flex items-end gap-4 pb-1">
								<label class="flex items-center gap-1.5 text-sm">
									<Checkbox
										checked={properties[i].multivalued ?? false}
										onCheckedChange={(v) => {
											properties[i].multivalued = !!v;
											properties = properties;
										}}
									/>
									Multi
									<Tooltip.Root>
										<Tooltip.Trigger>
											{#snippet child({ props })}
												<span {...props}><HelpCircle class="h-3 w-3 text-muted-foreground" /></span>
											{/snippet}
										</Tooltip.Trigger>
										<Tooltip.Content>Field can contain multiple values</Tooltip.Content>
									</Tooltip.Root>
								</label>
							</div>
						</div>
						{#if properties[i].type === 'DATETIME'}
							<div class="mt-2 max-w-xs space-y-1">
								<Label class="text-xs">Format</Label>
								<Input
									class="h-8 text-sm"
									placeholder="yyyy-MM-dd"
									bind:value={properties[i].format}
								/>
							</div>
						{/if}
					</div>
					<Button
						variant="ghost"
						size="icon"
						class="h-8 w-8 shrink-0 text-muted-foreground hover:text-destructive"
						onclick={() => removeProperty(i)}
					>
						<Trash2 class="h-3.5 w-3.5" />
					</Button>
				</div>
			{/each}
		</div>
	{:else}
		<p class="mt-3 text-sm text-muted-foreground">
			No properties defined. Add properties to define which metadata fields to index.
		</p>
	{/if}
</div>
