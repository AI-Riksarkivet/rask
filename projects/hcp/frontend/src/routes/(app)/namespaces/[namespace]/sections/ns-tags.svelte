<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Pencil } from 'lucide-svelte';
	import ServiceTagBadge from '$lib/components/custom/service-tag-badge/service-tag-badge.svelte';
	import SaveButton from '$lib/components/custom/save-button/save-button.svelte';
	import TagInput from '$lib/components/custom/tag-input/tag-input.svelte';
	import { toast } from 'svelte-sonner';
	import {
		get_namespace,
		update_namespace,
		type Namespace,
	} from '$lib/remote/namespaces.remote.js';

	let {
		tenant,
		namespaceName,
	}: {
		tenant: string;
		namespaceName: string;
	} = $props();

	let nsData = $derived(get_namespace({ tenant, name: namespaceName }));
	let ns = $derived((nsData?.current ?? null) as Namespace | null);

	let editingTags = $state(false);
	let editTags = $state<string[]>([]);
	let savingTags = $state(false);

	function startEditTags() {
		editTags = [...(ns?.tags?.tag ?? [])];
		editingTags = true;
	}

	async function saveTags() {
		if (!nsData) return;
		savingTags = true;
		try {
			await update_namespace({
				tenant,
				name: namespaceName,
				body: { tags: { tag: editTags } },
			}).updates(nsData);
			toast.success('Tags updated');
			editingTags = false;
		} catch {
			toast.error('Failed to update tags');
		} finally {
			savingTags = false;
		}
	}
</script>

<Card.Root class="flex h-full flex-col">
	<Card.Header class="pb-3">
		<Card.Title class="text-base">Tags</Card.Title>
		<Card.Description>Classify and organize namespaces with custom labels.</Card.Description>
		{#if !editingTags}
			<Card.Action>
				<Button variant="ghost" size="icon" class="h-6 w-6" onclick={startEditTags}>
					<Pencil class="h-3.5 w-3.5" />
				</Button>
			</Card.Action>
		{/if}
	</Card.Header>
	<Card.Content>
		{#if editingTags}
			<TagInput bind:tags={editTags} />
		{:else if ns?.tags?.tag?.length}
			<div class="flex flex-wrap gap-1.5">
				{#each ns.tags.tag as t (t)}
					<ServiceTagBadge tag={t} />
				{/each}
			</div>
		{:else}
			<p class="text-sm text-muted-foreground">No tags</p>
		{/if}
	</Card.Content>
	{#if editingTags}
		<Card.Footer class="gap-3">
			<SaveButton dirty={true} saving={savingTags} onclick={saveTags} />
			<Button variant="ghost" size="sm" onclick={() => (editingTags = false)}>Cancel</Button>
		</Card.Footer>
	{/if}
</Card.Root>
