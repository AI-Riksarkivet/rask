<script lang="ts">
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import { Trash2 } from 'lucide-svelte';
	import { Button } from '$lib/components/ui/button/index.js';
	import { toast } from 'svelte-sonner';
	import DeleteConfirmDialog from '$lib/components/custom/delete-confirm-dialog/delete-confirm-dialog.svelte';
	import BackButton from '$lib/components/custom/back-button/back-button.svelte';
	import SaveButton from '$lib/components/custom/save-button/save-button.svelte';
	import NoTenantPlaceholder from '$lib/components/custom/no-tenant-placeholder/no-tenant-placeholder.svelte';
	import {
		get_content_class,
		update_content_class,
		delete_content_class,
		type ContentClass,
		type ContentProperty,
	} from '$lib/remote/content-classes.remote.js';
	import CcProperties from './sections/cc-properties.svelte';
	import CcNamespaces from './sections/cc-namespaces.svelte';

	let tenant = $derived(page.data.tenant as string | undefined);
	let className = $derived(page.params.name ?? '');

	let classData = $derived(
		tenant && className ? get_content_class({ tenant, name: className }) : undefined
	);
	let contentClass = $derived((classData?.current ?? null) as ContentClass | null);

	// Local editable state
	let syncVersion = $state(0);
	let localProperties = $state<ContentProperty[]>([]);
	let localNamespaces = $state<string[]>([]);

	$effect(() => {
		const cc = contentClass;
		void syncVersion;
		localProperties = (cc?.contentProperties ?? []).map((p) => ({ ...p }));
		localNamespaces = [...(cc?.namespaces ?? [])];
	});

	function propsEqual(): boolean {
		const remote = contentClass?.contentProperties ?? [];
		if (localProperties.length !== remote.length) return false;
		return localProperties.every(
			(p, i) =>
				p.name === remote[i].name &&
				p.expression === remote[i].expression &&
				p.type === remote[i].type &&
				(p.multivalued ?? false) === (remote[i].multivalued ?? false) &&
				(p.format ?? '') === (remote[i].format ?? '')
		);
	}

	let dirty = $derived(
		!propsEqual() ||
			JSON.stringify([...localNamespaces].sort()) !==
				JSON.stringify([...(contentClass?.namespaces ?? [])].sort())
	);

	let saving = $state(false);

	async function save() {
		if (!tenant || !classData) return;
		saving = true;
		try {
			await update_content_class({
				tenant,
				name: className,
				body: {
					contentProperties: localProperties,
					namespaces: localNamespaces,
				},
			}).updates(classData);
			syncVersion++;
			toast.success('Content class updated');
		} catch {
			toast.error('Failed to update content class');
		} finally {
			saving = false;
		}
	}

	// Delete
	let deleteOpen = $state(false);
	let deleting = $state(false);

	async function handleDelete() {
		if (!tenant) return;
		deleting = true;
		try {
			await delete_content_class({ tenant, name: className });
			toast.success(`Content class "${className}" deleted`);
			goto('/content-classes');
		} catch {
			toast.error('Failed to delete content class');
			deleting = false;
		}
	}
</script>

<svelte:head>
	<title>{className} - Content Class - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<!-- Header -->
	<div class="flex items-center justify-between">
		<div class="flex items-center gap-4">
			<BackButton href="/content-classes" label="Back to content classes" />
			<div>
				<h2 class="text-2xl font-bold">{className}</h2>
				<p class="mt-1 text-sm text-muted-foreground">Metadata schema for object indexing</p>
			</div>
		</div>
		{#if contentClass}
			<Button variant="destructive" size="sm" onclick={() => (deleteOpen = true)}>
				<Trash2 class="h-4 w-4" />
				Delete
			</Button>
		{/if}
	</div>

	{#if !tenant}
		<NoTenantPlaceholder message="Log in with a tenant to view content class details." />
	{:else}
		{#await classData}
			<div class="rounded-lg border p-5">
				<div class="space-y-3">
					{#each Array(3) as _, i (i)}
						<div class="h-8 w-full animate-pulse rounded bg-muted"></div>
					{/each}
				</div>
			</div>
		{:then}
			{#if !contentClass}
				<div class="rounded-lg border border-dashed p-8 text-center">
					<p class="text-muted-foreground">Content class not found or could not be loaded.</p>
				</div>
			{:else}
				<CcProperties bind:properties={localProperties} />
				<CcNamespaces bind:namespaces={localNamespaces} />
				<SaveButton {dirty} {saving} onclick={save} />
			{/if}
		{/await}
	{/if}
</div>

<DeleteConfirmDialog
	bind:open={deleteOpen}
	name={className}
	itemType="content class"
	loading={deleting}
	onconfirm={handleDelete}
/>
