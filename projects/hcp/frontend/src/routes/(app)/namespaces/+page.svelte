<script lang="ts">
	import { page } from '$app/state';
	import { Plus, Database, FileJson, BarChart3 } from 'lucide-svelte';
	import { Button } from '$lib/components/ui/button/index.js';
	import * as Tabs from '$lib/components/ui/tabs/index.js';
	import PageHeader from '$lib/components/custom/page-header/page-header.svelte';
	import NoTenantPlaceholder from '$lib/components/custom/no-tenant-placeholder/no-tenant-placeholder.svelte';
	import NamespaceStats from './sections/namespace-stats.svelte';
	import NamespaceTable from './sections/namespace-table.svelte';
	import NamespaceCreateDialog from './sections/namespace-create-dialog.svelte';
	import NsGrantDialog from './sections/ns-grant-dialog.svelte';
	import NsTemplates from './sections/ns-templates.svelte';
	import TenantChargeback from './sections/tenant-chargeback.svelte';
	import { get_namespaces } from '$lib/remote/namespaces.remote.js';
	import { get_tenant_chargeback } from '$lib/remote/tenant-info.remote.js';

	let tenant = $derived(page.data.tenant as string | undefined);

	let nsData = $derived(tenant ? get_namespaces({ tenant }) : undefined);
	// Chargeback is fetched on demand — only when the user switches to the chargeback
	// tab or when it's needed by stats. Cached for 60s by the backend (CACHE_STATS_TTL).
	let chargebackData = $derived(tenant ? get_tenant_chargeback({ tenant }) : undefined);

	let activeTab = $state('namespaces');
	let createOpen = $state(false);
	let grantOpen = $state(false);
	let grantNamespaces = $state<string[]>([]);

	function handleGrantAccess(namespaceNames: string[]) {
		grantNamespaces = namespaceNames;
		grantOpen = true;
	}
</script>

<svelte:head>
	<title>Namespaces - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<PageHeader title="Namespaces" description="Manage tenant namespaces">
		{#snippet actions()}
			{#if tenant && activeTab === 'namespaces'}
				<Button onclick={() => (createOpen = true)}>
					<Plus class="h-4 w-4" />
					Create Namespace
				</Button>
			{/if}
		{/snippet}
	</PageHeader>

	{#if tenant && nsData && chargebackData}
		<Tabs.Root bind:value={activeTab}>
			<Tabs.List>
				<Tabs.Trigger value="namespaces">
					<Database class="mr-1.5 h-4 w-4" />
					Namespaces
				</Tabs.Trigger>
				<Tabs.Trigger value="chargeback">
					<BarChart3 class="mr-1.5 h-4 w-4" />
					Chargeback
				</Tabs.Trigger>
				<Tabs.Trigger value="templates">
					<FileJson class="mr-1.5 h-4 w-4" />
					Templates
				</Tabs.Trigger>
			</Tabs.List>

			<Tabs.Content value="namespaces" class="space-y-6">
				<NamespaceStats {tenant} {nsData} {chargebackData} />
				<NamespaceTable {tenant} {nsData} {chargebackData} ongrantaccess={handleGrantAccess} />
			</Tabs.Content>

			<Tabs.Content value="chargeback">
				<TenantChargeback {chargebackData} />
			</Tabs.Content>

			<Tabs.Content value="templates">
				<NsTemplates {tenant} {nsData} />
			</Tabs.Content>
		</Tabs.Root>

		<NamespaceCreateDialog {tenant} {nsData} bind:open={createOpen} />
		<NsGrantDialog {tenant} namespaceNames={grantNamespaces} bind:open={grantOpen} />
	{:else if !tenant}
		<NoTenantPlaceholder message="Log in with a tenant to manage namespaces." />
	{/if}
</div>
