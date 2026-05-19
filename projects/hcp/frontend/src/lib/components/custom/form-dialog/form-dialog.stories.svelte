<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { fn } from 'storybook/test';
	import FormDialog from './form-dialog.svelte';

	const { Story } = defineMeta({
		title: 'UI/FormDialog',
		component: FormDialog,
		args: {
			title: 'Create Namespace',
			description: 'Create a new namespace in the tenant.',
			submitLabel: 'Create',
			loading: false,
			error: '',
			onsubmit: fn(),
		},
	});
</script>

<script lang="ts">
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Button } from '$lib/components/ui/button/index.js';

	let defaultOpen = $state(false);
	let errorOpen = $state(false);
	let loadingOpen = $state(false);
</script>

<Story name="Default">
	{#snippet template(args)}
		<Button onclick={() => (defaultOpen = true)}>Open Dialog</Button>
		<FormDialog
			bind:open={defaultOpen}
			title={args.title}
			description={args.description}
			submitLabel={args.submitLabel}
			loading={args.loading}
			error={args.error}
			onsubmit={(e) => {
				e.preventDefault();
				args.onsubmit?.(e);
				defaultOpen = false;
			}}
		>
			<div class="space-y-3">
				<div>
					<Label>Name</Label>
					<Input placeholder="my-namespace" />
				</div>
				<div>
					<Label>Description</Label>
					<Input placeholder="Optional description..." />
				</div>
			</div>
		</FormDialog>
	{/snippet}
</Story>

<Story name="With Error" args={{ error: "Namespace 'my-namespace' already exists." }}>
	{#snippet template(args)}
		<Button onclick={() => (errorOpen = true)}>Open With Error</Button>
		<FormDialog
			bind:open={errorOpen}
			title={args.title}
			description={args.description}
			submitLabel={args.submitLabel}
			loading={args.loading}
			error={args.error}
			onsubmit={(e) => {
				e.preventDefault();
				args.onsubmit?.(e);
			}}
		>
			<div>
				<Label>Name</Label>
				<Input value="my-namespace" />
			</div>
		</FormDialog>
	{/snippet}
</Story>

<Story name="Loading" args={{ loading: true }}>
	{#snippet template(args)}
		<Button onclick={() => (loadingOpen = true)}>Open Loading</Button>
		<FormDialog
			bind:open={loadingOpen}
			title={args.title}
			description={args.description}
			submitLabel={args.submitLabel}
			loading={args.loading}
			error={args.error}
			onsubmit={(e) => {
				e.preventDefault();
				args.onsubmit?.(e);
			}}
		>
			<div>
				<Label>Name</Label>
				<Input value="my-namespace" />
			</div>
		</FormDialog>
	{/snippet}
</Story>
