<script lang="ts">
	import { Switch } from '$lib/components/ui/switch/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import * as Card from '$lib/components/ui/card/index.js';
	import SaveButton from '$lib/components/custom/save-button/save-button.svelte';
	import { useSave } from '$lib/utils/use-save.svelte.js';
	import {
		get_ns_indexing,
		update_ns_indexing,
		type IndexingSettings,
	} from '$lib/remote/namespaces.remote.js';

	let {
		tenant,
		namespaceName,
	}: {
		tenant: string;
		namespaceName: string;
	} = $props();

	let indexingData = $derived(get_ns_indexing({ tenant, name: namespaceName }));
	let indexing = $derived((indexingData?.current ?? {}) as IndexingSettings);

	const saver = useSave({
		successMsg: 'Indexing settings updated',
		errorMsg: 'Failed to update indexing settings',
	});

	let localFullIndexing = $state(false);
	let localContentClasses = $state('');
	let localExcludedAnnotations = $state('');

	$effect(() => {
		const idx = indexing;
		void saver.syncVersion;
		localFullIndexing = idx.fullIndexingEnabled ?? false;
		localContentClasses = (idx.contentClasses ?? []).join(', ');
		localExcludedAnnotations = idx.excludedAnnotations ?? '';
	});

	let dirty = $derived(
		localFullIndexing !== (indexing.fullIndexingEnabled ?? false) ||
			localContentClasses !== (indexing.contentClasses ?? []).join(', ') ||
			localExcludedAnnotations !== (indexing.excludedAnnotations ?? '')
	);
</script>

<Card.Root class="flex h-full flex-col">
	<Card.Header>
		<Card.Title>Custom Metadata Indexing</Card.Title>
		<Card.Description>
			Configure how the metadata query engine indexes custom metadata for search.
		</Card.Description>
	</Card.Header>
	{#await indexingData}
		<Card.Content class="flex-1">
			<div class="flex flex-wrap gap-6">
				{#each Array(3) as _, i (i)}
					<div class="h-5 w-28 animate-pulse rounded bg-muted"></div>
				{/each}
			</div>
		</Card.Content>
	{:then}
		<Card.Content class="flex-1 space-y-4">
			<div class="space-y-1">
				<div class="flex items-center gap-3">
					<Switch id="full-indexing" bind:checked={localFullIndexing} />
					<Label for="full-indexing">Full Indexing</Label>
				</div>
				<p class="text-xs text-muted-foreground pl-11">
					Enable full-text search across all custom metadata fields.
				</p>
			</div>
			<div class="grid gap-4 sm:grid-cols-2">
				<div class="space-y-2">
					<Label for="content-classes">Content Classes</Label>
					<Input
						id="content-classes"
						placeholder="class1, class2"
						bind:value={localContentClasses}
					/>
					<p class="text-xs text-muted-foreground">
						Comma-separated content classes defining which metadata types are indexed.
					</p>
				</div>
				<div class="space-y-2">
					<Label for="excluded-annotations">Excluded Annotations</Label>
					<Input
						id="excluded-annotations"
						placeholder="annotation names"
						bind:value={localExcludedAnnotations}
					/>
					<p class="text-xs text-muted-foreground">
						Annotations to exclude from indexing. Supports wildcards (e.g. misc*).
					</p>
				</div>
			</div>
		</Card.Content>
		<Card.Footer>
			<SaveButton
				{dirty}
				saving={saver.saving}
				onclick={() =>
					saver.run(async () => {
						if (!indexingData) return;
						const classes = localContentClasses
							.split(',')
							.map((s) => s.trim())
							.filter(Boolean);
						await update_ns_indexing({
							tenant,
							name: namespaceName,
							body: {
								fullIndexingEnabled: localFullIndexing,
								contentClasses: classes.length > 0 ? classes : undefined,
								excludedAnnotations: localExcludedAnnotations || undefined,
							},
						}).updates(indexingData);
					})}
			/>
		</Card.Footer>
	{/await}
</Card.Root>
