<script lang="ts">
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { toast } from 'svelte-sonner';
	import FormDialog from '$lib/components/custom/form-dialog/form-dialog.svelte';
	import { get_buckets, create_bucket } from '$lib/remote/buckets.remote.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let {
		open = $bindable(false),
	}: {
		open: boolean;
	} = $props();

	let bucketData = get_buckets();
	let createError = $state('');
	let creating = $state(false);

	const BUCKET_NAME_RE = /^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$/;
	const BUCKET_NAME_HINT =
		'Use only lowercase letters, numbers, hyphens, and dots. Must be 3–63 characters.';

	function validateBucketName(name: string): string | null {
		if (name.length < 3) return 'Bucket name must be at least 3 characters.';
		if (name.length > 63) return 'Bucket name must be at most 63 characters.';
		if (!BUCKET_NAME_RE.test(name)) return BUCKET_NAME_HINT;
		return null;
	}

	async function handleCreate(e: SubmitEvent) {
		e.preventDefault();
		const form = e.currentTarget as HTMLFormElement;
		const formData = new FormData(form);
		const name = (formData.get('bucket') as string).trim();
		if (!name) return;
		const validationError = validateBucketName(name);
		if (validationError) {
			createError = validationError;
			return;
		}
		creating = true;
		createError = '';
		try {
			const result = await create_bucket({ bucket: name }).updates(bucketData);
			if (result?.error) {
				createError = result.error;
			} else {
				toast.success('Bucket created successfully');
				open = false;
				form.reset();
			}
		} catch (err) {
			createError = getErrorMessage(err, 'Failed to create bucket');
		} finally {
			creating = false;
		}
	}
</script>

<FormDialog
	bind:open
	title="Create Bucket"
	loading={creating}
	error={createError}
	onsubmit={handleCreate}
	class="sm:max-w-md"
>
	<div class="space-y-2">
		<Label for="bucket-name">Bucket Name</Label>
		<Input id="bucket-name" name="bucket" placeholder="my-bucket" required />
		<p class="text-muted-foreground text-xs">
			Lowercase letters, numbers, hyphens, and dots only. 3–63 characters.
		</p>
	</div>
</FormDialog>
