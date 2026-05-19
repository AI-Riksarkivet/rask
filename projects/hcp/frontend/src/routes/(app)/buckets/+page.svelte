<script lang="ts">
	import { Plus } from 'lucide-svelte';
	import { Button } from '$lib/components/ui/button/index.js';
	import PageHeader from '$lib/components/custom/page-header/page-header.svelte';
	import BucketTable from './sections/bucket-table.svelte';
	import BucketCreateDialog from './sections/bucket-create-dialog.svelte';

	let createOpen = $state(false);
</script>

<svelte:head>
	<title>Buckets - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<PageHeader title="Buckets" description="Manage S3 buckets on your HCP system">
		{#snippet actions()}
			<Button onclick={() => (createOpen = true)}>
				<Plus class="h-4 w-4" />
				Create Bucket
			</Button>
		{/snippet}
	</PageHeader>

	<BucketTable />
</div>

<BucketCreateDialog bind:open={createOpen} />
