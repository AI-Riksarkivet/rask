<script lang="ts">
	import FormDialog from '$lib/components/custom/form-dialog/form-dialog.svelte';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { create_folder } from '$lib/remote/buckets.remote.js';
	import { toast } from 'svelte-sonner';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let {
		open = $bindable(false),
		bucket,
		prefix,
		oncreated,
	}: {
		open: boolean;
		bucket: string;
		prefix: string;
		oncreated: () => void;
	} = $props();

	let folderName = $state('');
	let creating = $state(false);
	let createError = $state('');

	let fullPath = $derived(`${prefix}${folderName}/`);

	function validate(name: string): string | null {
		if (!name.trim()) return 'Folder name is required.';
		if (name.includes('//')) return 'Folder name must not contain double slashes.';
		return null;
	}

	async function handleSubmit(e: SubmitEvent) {
		e.preventDefault();
		const trimmed = folderName.trim();
		const validationError = validate(trimmed);
		if (validationError) {
			createError = validationError;
			return;
		}
		creating = true;
		createError = '';
		try {
			await create_folder({ bucket, folder_name: `${prefix}${trimmed}/` });
			toast.success(`Folder "${trimmed}" created`);
			open = false;
			folderName = '';
			oncreated();
		} catch (err) {
			createError = getErrorMessage(err, 'Failed to create folder');
		} finally {
			creating = false;
		}
	}
</script>

<FormDialog
	bind:open
	title="Create Folder"
	loading={creating}
	error={createError}
	onsubmit={handleSubmit}
>
	<div class="space-y-2">
		<Label for="folder-name">Folder Name</Label>
		<Input id="folder-name" placeholder="new-folder" bind:value={folderName} required />
		{#if folderName.trim()}
			<p class="text-muted-foreground break-all text-xs">
				Will create: <span class="font-mono font-medium">{fullPath}</span>
			</p>
		{/if}
	</div>
</FormDialog>
