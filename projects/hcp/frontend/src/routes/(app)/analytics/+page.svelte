<script lang="ts">
	import { page } from '$app/state';
	import PageHeader from '$lib/components/custom/page-header/page-header.svelte';
	import NoTenantPlaceholder from '$lib/components/custom/no-tenant-placeholder/no-tenant-placeholder.svelte';
	import AnalyticsSidebar from './sections/analytics-sidebar.svelte';
	import AnalyticsTable from './sections/analytics-table.svelte';
	import { Table2 } from 'lucide-svelte';

	let tenant = $derived(page.data.tenant as string | undefined);

	let selectedBucket = $state('');
	let selectedPath = $state('');
	let selectedTable = $state('');
</script>

{#if tenant}
	<PageHeader title="Data Explorer" description="Browse Lance datasets on S3">
		{#snippet actions()}
			<div class="flex items-center gap-2 text-sm text-muted-foreground">
				<Table2 class="h-4 w-4" />
				LanceDB
			</div>
		{/snippet}
	</PageHeader>

	<div class="grid grid-cols-[320px_1fr] gap-6">
		<AnalyticsSidebar {tenant} bind:selectedBucket bind:selectedPath bind:selectedTable />

		<div>
			{#if selectedTable}
				<AnalyticsTable bucket={selectedBucket} path={selectedPath} table={selectedTable} />
			{:else if selectedBucket}
				<div class="py-12 text-center text-muted-foreground">
					Select a table from the sidebar to browse its data.
				</div>
			{:else}
				<div class="py-12 text-center text-muted-foreground">
					Enter a bucket name and path to discover Lance datasets.
				</div>
			{/if}
		</div>
	</div>
{:else}
	<NoTenantPlaceholder message="Log in with a tenant to explore datasets." />
{/if}
