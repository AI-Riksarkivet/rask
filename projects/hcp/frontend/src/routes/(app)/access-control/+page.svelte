<script lang="ts">
	import { page } from '$app/state';
	import { Shield, Plus } from 'lucide-svelte';
	import { Button } from '$lib/components/ui/button/index.js';
	import PageHeader from '$lib/components/custom/page-header/page-header.svelte';
	import AclTable from './sections/acl-table.svelte';
	import AclGrantDialog from './sections/acl-grant-dialog.svelte';

	let tenant = $derived(page.data.tenant as string | undefined);
	let grantOpen = $state(false);
</script>

<svelte:head>
	<title>Access Control - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<PageHeader title="Access Control" description="Overview of S3 bucket access control lists">
		{#snippet actions()}
			<div class="flex items-center gap-2">
				<Shield class="h-5 w-5 text-muted-foreground" />
				<Button size="sm" onclick={() => (grantOpen = true)}>
					<Plus class="h-4 w-4" />
					Grant Access
				</Button>
			</div>
		{/snippet}
	</PageHeader>

	<AclTable {tenant} />
</div>

<AclGrantDialog {tenant} bind:open={grantOpen} />
