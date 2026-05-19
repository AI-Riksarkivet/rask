<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import PageHeader from '$lib/components/custom/page-header/page-header.svelte';
	import { get_support_credentials, type SupportCredentials } from '$lib/remote/system.remote.js';

	let credData = $derived(get_support_credentials({}));
	let creds = $derived((credData?.current ?? {}) as SupportCredentials);

	function formatTimestamp(ts?: number): string {
		if (!ts) return '—';
		return new Date(ts).toLocaleString();
	}
</script>

<svelte:head>
	<title>Support - System - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<PageHeader
		title="Support Access Credentials"
		description="View current support access credential information"
	/>

	{#await credData}
		<CardSkeleton />
	{:then}
		<Card.Root class="max-w-2xl">
			<Card.Header>
				<Card.Title>Credential Details</Card.Title>
				<Card.Description>Current support access SSH key information.</Card.Description>
			</Card.Header>
			<Card.Content>
				<dl class="grid grid-cols-[auto_1fr] gap-x-6 gap-y-3 text-sm">
					<dt class="text-muted-foreground">Key Type</dt>
					<dd>
						<Badge variant="outline">{creds.type ?? '—'}</Badge>
					</dd>

					<dt class="text-muted-foreground">Default Key Type</dt>
					<dd>{creds.defaultKeyType ?? '—'}</dd>

					<dt class="text-muted-foreground">Serial Number</dt>
					<dd class="font-mono">{creds.serialNumberFromPackage ?? '—'}</dd>

					<dt class="text-muted-foreground">Created</dt>
					<dd>{formatTimestamp(creds.createTimeStamp)}</dd>

					<dt class="text-muted-foreground">Applied</dt>
					<dd>{formatTimestamp(creds.applyTimeStamp)}</dd>
				</dl>
			</Card.Content>
		</Card.Root>
	{/await}
</div>
