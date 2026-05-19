<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import PageHeader from './page-header.svelte';

	const { Story } = defineMeta({
		title: 'UI/PageHeader',
		component: PageHeader,
		args: {
			title: 'Namespaces',
			description: 'Manage tenant namespaces',
		},
	});
</script>

<script lang="ts">
	import { Button } from '$lib/components/ui/button/index.js';
	import { Plus, Download, Trash2 } from 'lucide-svelte';
</script>

<Story name="Default" args={{ title: 'Namespaces', description: 'Manage tenant namespaces' }} />

<Story name="With Single Action">
	{#snippet template(args)}
		<PageHeader title={args.title} description={args.description}>
			{#snippet actions()}
				<Button size="sm">
					<Plus class="h-4 w-4" />
					Create Namespace
				</Button>
			{/snippet}
		</PageHeader>
	{/snippet}
</Story>

<Story
	name="With Multiple Actions"
	args={{ title: 'Buckets', description: 'Manage S3-compatible storage buckets' }}
>
	{#snippet template(args)}
		<PageHeader title={args.title} description={args.description}>
			{#snippet actions()}
				<div class="flex gap-2">
					<Button size="sm" variant="outline">
						<Download class="h-4 w-4" />
						Export
					</Button>
					<Button size="sm" variant="destructive">
						<Trash2 class="h-4 w-4" />
						Delete Selected
					</Button>
					<Button size="sm">
						<Plus class="h-4 w-4" />
						Create Bucket
					</Button>
				</div>
			{/snippet}
		</PageHeader>
	{/snippet}
</Story>

<Story
	name="Long Description"
	args={{
		title: 'Settings',
		description:
			'Configure tenant-wide settings including authentication, storage quotas, and network policies.',
	}}
/>

<Story
	name="Short Title"
	args={{
		title: 'Users',
		description: 'Manage user accounts and group memberships for this tenant.',
	}}
/>
