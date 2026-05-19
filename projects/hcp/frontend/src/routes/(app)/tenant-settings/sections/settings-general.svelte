<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import { get_tenant, type TenantInfo } from '$lib/remote/tenant-info.remote.js';

	let {
		tenant,
	}: {
		tenant: string;
	} = $props();

	let tenantData = $derived(get_tenant({ tenant }));
	let info = $derived((tenantData?.current ?? null) as TenantInfo | null);
</script>

{#await tenantData}
	<CardSkeleton />
{:then}
	<Card.Root>
		<Card.Header>
			<Card.Title>General</Card.Title>
			<Card.Description>Core tenant configuration</Card.Description>
		</Card.Header>
		<Card.Content>
			{#if info}
				<dl class="space-y-3">
					<div class="flex justify-between">
						<dt class="text-sm text-muted-foreground">Tenant Name</dt>
						<dd class="text-sm font-medium">{info.name}</dd>
					</div>
					<div class="flex justify-between">
						<dt class="text-sm text-muted-foreground">HCP Version</dt>
						<dd class="text-sm font-medium">{info.softwareVersion ?? '—'}</dd>
					</div>
					<div class="flex justify-between">
						<dt class="text-sm text-muted-foreground">Namespace Quota</dt>
						<dd class="text-sm font-medium">{info.namespaceQuota ?? '—'}</dd>
					</div>
					<div class="flex justify-between">
						<dt class="text-sm text-muted-foreground">Hard Quota</dt>
						<dd class="text-sm font-medium">{info.hardQuota ?? '—'}</dd>
					</div>
					<div class="flex justify-between">
						<dt class="text-sm text-muted-foreground">Soft Quota</dt>
						<dd class="text-sm font-medium">
							{info.softQuota != null ? `${info.softQuota}%` : '—'}
						</dd>
					</div>
					<div class="flex justify-between">
						<dt class="text-sm text-muted-foreground">Authentication</dt>
						<dd class="flex gap-1">
							{#each info.authenticationTypes?.authenticationType ?? [] as type (type)}
								<Badge variant="secondary">{type}</Badge>
							{/each}
							{#if !info.authenticationTypes?.authenticationType?.length}
								<span class="text-sm text-muted-foreground">—</span>
							{/if}
						</dd>
					</div>
					<div class="flex justify-between">
						<dt class="text-sm text-muted-foreground">Service Plan</dt>
						<dd class="text-sm font-medium">{info.servicePlan ?? '—'}</dd>
					</div>
				</dl>
			{/if}
		</Card.Content>
	</Card.Root>
{/await}
