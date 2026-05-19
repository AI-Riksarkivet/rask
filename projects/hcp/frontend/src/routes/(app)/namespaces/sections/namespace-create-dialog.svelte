<script lang="ts">
	import { Checkbox } from '$lib/components/ui/checkbox/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Switch } from '$lib/components/ui/switch/index.js';
	import { toast } from 'svelte-sonner';
	import FormDialog from '$lib/components/custom/form-dialog/form-dialog.svelte';
	import TagInput from '$lib/components/custom/tag-input/tag-input.svelte';
	import type { RemoteQuery } from '@sveltejs/kit';
	import { create_namespace, type Namespace } from '$lib/remote/namespaces.remote.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let {
		tenant,
		nsData,
		open = $bindable(false),
	}: {
		tenant: string;
		nsData: RemoteQuery<Namespace[]>;
		open: boolean;
	} = $props();

	let createError = $state('');
	let creating = $state(false);
	let createHashScheme = $state('SHA-256');
	let createTags = $state<string[]>([]);
	let createOptimizedFor = $state('CLOUD');
	let versioningEnabled = $state(false);
	let keepDeletionRecords = $state(false);

	async function handleCreate(e: SubmitEvent) {
		e.preventDefault();
		if (!nsData) return;
		const form = e.currentTarget as HTMLFormElement;
		const formData = new FormData(form);
		const name = formData.get('namespace') as string;
		if (!name) return;
		creating = true;
		createError = '';
		try {
			const description = (formData.get('description') as string) || undefined;
			const hardQuota = (formData.get('hardQuota') as string) || undefined;
			const softQuotaStr = formData.get('softQuota') as string;
			const softQuota = softQuotaStr ? Number(softQuotaStr) : undefined;
			const hashScheme = (formData.get('hashScheme') as string) || undefined;
			const searchEnabled = formData.has('searchEnabled');
			const owner = (formData.get('owner') as string) || undefined;
			await create_namespace({
				tenant,
				name,
				description,
				hardQuota,
				softQuota,
				hashScheme,
				searchEnabled,
				versioningEnabled,
				keepDeletionRecords: versioningEnabled ? keepDeletionRecords : undefined,
				optimizedFor: createOptimizedFor || undefined,
				tags: createTags.length > 0 ? createTags : undefined,
				owner,
			}).updates(nsData);
			toast.success('Namespace created successfully');
			open = false;
			createTags = [];
			form.reset();
			createOptimizedFor = 'CLOUD';
			versioningEnabled = false;
			keepDeletionRecords = false;
		} catch (err) {
			createError = getErrorMessage(err, 'Failed to create namespace');
		} finally {
			creating = false;
		}
	}
</script>

<FormDialog
	bind:open
	title="Create Namespace"
	description="Configure a new namespace for this tenant."
	loading={creating}
	error={createError}
	onsubmit={handleCreate}
>
	<div class="space-y-2">
		<Label for="ns-name">Namespace Name</Label>
		<Input id="ns-name" name="namespace" placeholder="my-namespace" required />
	</div>
	<div class="space-y-2">
		<Label for="ns-desc">Description</Label>
		<Input id="ns-desc" name="description" placeholder="Optional description" />
	</div>
	<div class="space-y-2">
		<Label for="ns-owner">Owner</Label>
		<Input id="ns-owner" name="owner" placeholder="HCP username (optional)" />
		<p class="text-xs text-muted-foreground">The owner sees this namespace as an S3 bucket.</p>
	</div>
	<div class="grid grid-cols-2 gap-4">
		<div class="space-y-2">
			<Label for="ns-hard-quota">Hard Quota</Label>
			<Input id="ns-hard-quota" name="hardQuota" placeholder="e.g. 50 GB" />
		</div>
		<div class="space-y-2">
			<Label for="ns-soft-quota">Soft Quota (%)</Label>
			<Input id="ns-soft-quota" name="softQuota" type="number" min="10" max="95" placeholder="85" />
		</div>
	</div>
	<div class="grid grid-cols-2 gap-4">
		<div class="space-y-2">
			<Label for="ns-hash">Hash Scheme</Label>
			<select
				id="ns-hash"
				class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-9 w-full items-center rounded-md border px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
				bind:value={createHashScheme}
			>
				<option value="SHA-256">SHA-256</option>
				<option value="SHA-512">SHA-512</option>
				<option value="SHA-384">SHA-384</option>
				<option value="SHA-1">SHA-1</option>
				<option value="MD5">MD5</option>
			</select>
			<input type="hidden" name="hashScheme" value={createHashScheme} />
		</div>
		<div class="space-y-2">
			<Label for="ns-optimized">Optimized For</Label>
			<select
				id="ns-optimized"
				class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-9 w-full items-center rounded-md border px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
				bind:value={createOptimizedFor}
			>
				<option value="CLOUD">Cloud</option>
				<option value="ALL">All</option>
			</select>
			<p class="text-xs text-muted-foreground">Cannot be changed after creation.</p>
		</div>
	</div>
	<div class="space-y-2">
		<Label>Tags</Label>
		<TagInput bind:tags={createTags} placeholder="e.g. lakefs, nfs, s3" />
	</div>
	<div class="space-y-3">
		<div class="flex gap-6">
			<div class="flex items-center gap-2">
				<Checkbox id="ns-search" name="searchEnabled" />
				<Label for="ns-search">Enable Search</Label>
			</div>
			<div class="flex items-center gap-2">
				<Switch id="ns-versioning" bind:checked={versioningEnabled} />
				<Label for="ns-versioning">Enable Versioning</Label>
			</div>
		</div>
		{#if versioningEnabled}
			<div class="ml-6 space-y-2 rounded-md border bg-muted/30 p-3">
				<p class="text-xs font-medium text-muted-foreground">Versioning Settings</p>
				<div class="flex flex-col gap-2">
					<div class="flex items-center gap-2">
						<Switch id="ns-keep-deletion" bind:checked={keepDeletionRecords} />
						<Label for="ns-keep-deletion" class="text-sm">Keep Deletion Records</Label>
					</div>
					<p class="ml-9 text-xs text-muted-foreground">
						Retain records of delete operations. Prevents namespace deletion when records exist.
					</p>
				</div>
			</div>
		{/if}
	</div>
</FormDialog>
