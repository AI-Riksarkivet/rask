<script lang="ts">
	import FormDialog from './form-dialog.svelte';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Button } from '$lib/components/ui/button/index.js';

	let open = $state(true);
	let error = $state('');
	let loading = $state(false);
	let submitted = $state(false);
	let nameValue = $state('');

	function handleSubmit(e: SubmitEvent) {
		e.preventDefault();
		if (!nameValue.trim()) {
			error = 'Name is required.';
			return;
		}
		if (nameValue === 'existing') {
			error = "Namespace 'existing' already exists.";
			return;
		}
		error = '';
		submitted = true;
	}

	function reopen() {
		open = true;
		error = '';
		submitted = false;
		nameValue = '';
	}
</script>

<div class="space-y-4 p-4">
	{#if !open}
		<Button onclick={reopen} data-testid="reopen-btn">Open Dialog</Button>
	{/if}

	{#if submitted}
		<div data-testid="success-msg" class="text-sm text-emerald-600">
			Created namespace: {nameValue}
		</div>
	{/if}

	<FormDialog
		bind:open
		title="Create Namespace"
		description="Create a new namespace."
		{loading}
		{error}
		onsubmit={handleSubmit}
	>
		<div class="space-y-3">
			<div>
				<Label for="ns-name">Name</Label>
				<Input id="ns-name" placeholder="my-namespace" bind:value={nameValue} />
			</div>
		</div>
	</FormDialog>
</div>
