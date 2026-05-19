<script lang="ts">
	import { X, Pencil } from 'lucide-svelte';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import ServiceTagBadge from '$lib/components/custom/service-tag-badge/service-tag-badge.svelte';

	let {
		tags,
		editing,
		onsave,
		onstartedit,
		oncanceledit,
	}: {
		tags: string[];
		editing: boolean;
		onsave: (tags: string[]) => void | Promise<void>;
		onstartedit: () => void;
		oncanceledit: () => void;
	} = $props();

	let editTags = $state<string[]>([]);
	let editTagInput = $state('');
	let saving = $state(false);

	$effect(() => {
		if (editing) {
			editTags = [...tags];
			editTagInput = '';
		}
	});

	function addEditTag() {
		const t = editTagInput.trim();
		if (t && !editTags.includes(t.toLowerCase())) {
			editTags = [...editTags, t.toLowerCase()];
		}
		editTagInput = '';
	}

	function removeEditTag(t: string) {
		editTags = editTags.filter((x) => x !== t);
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter') {
			e.preventDefault();
			addEditTag();
		}
	}

	async function handleSave() {
		saving = true;
		try {
			await onsave(editTags);
		} finally {
			saving = false;
		}
	}
</script>

{#if editing}
	<div
		class="flex flex-col gap-1.5"
		onclick={(e: MouseEvent) => e.stopPropagation()}
		onkeydown={(e: KeyboardEvent) => e.stopPropagation()}
		role="presentation"
	>
		<div class="flex gap-1.5">
			<Input
				class="h-7 w-28 px-2 text-xs"
				placeholder="Add tag..."
				bind:value={editTagInput}
				onkeydown={handleKeydown}
			/>
			<Button variant="ghost" size="icon" class="h-7 w-7" onclick={handleSave} disabled={saving}>
				{#if saving}...{:else}Save{/if}
			</Button>
			<Button variant="ghost" size="icon" class="h-7 w-7" onclick={oncanceledit}>
				<X class="h-3 w-3" />
			</Button>
		</div>
		{#if editTags.length > 0}
			<div class="flex flex-wrap gap-1">
				{#each editTags as t (t)}
					<span class="inline-flex items-center gap-0.5">
						<ServiceTagBadge tag={t} />
						<Button
							variant="ghost"
							size="icon"
							class="h-5 w-5 rounded-full text-muted-foreground hover:text-destructive"
							onclick={() => removeEditTag(t)}
						>
							<X class="h-2.5 w-2.5" />
						</Button>
					</span>
				{/each}
			</div>
		{/if}
	</div>
{:else}
	<div class="group flex items-center gap-1">
		{#if tags.length > 0}
			<div class="flex flex-wrap gap-1">
				{#each tags as t (t)}
					<ServiceTagBadge tag={t} />
				{/each}
			</div>
		{:else}
			<span class="text-muted-foreground">—</span>
		{/if}
		<Button
			variant="ghost"
			size="icon"
			class="h-6 w-6 rounded text-muted-foreground opacity-0 transition-opacity hover:text-foreground group-hover:opacity-100"
			onclick={(e) => {
				e.stopPropagation();
				onstartedit();
			}}
		>
			<Pencil class="h-3 w-3" />
		</Button>
	</div>
{/if}
