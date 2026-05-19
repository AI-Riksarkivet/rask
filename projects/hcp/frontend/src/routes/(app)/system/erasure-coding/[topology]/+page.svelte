<script lang="ts">
	import { page } from '$app/state';
	import * as Card from '$lib/components/ui/card/index.js';
	import * as Table from '$lib/components/ui/table/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import PageHeader from '$lib/components/custom/page-header/page-header.svelte';
	import BackButton from '$lib/components/custom/back-button/back-button.svelte';
	import FormDialog from '$lib/components/custom/form-dialog/form-dialog.svelte';
	import DeleteConfirmDialog from '$lib/components/custom/delete-confirm-dialog/delete-confirm-dialog.svelte';
	import { X } from 'lucide-svelte';
	import { toast } from 'svelte-sonner';
	import { formatBytes } from '$lib/utils/format.js';
	import {
		get_ec_topology,
		get_ec_tenant_candidates,
		add_tenant_to_ec_topology,
		remove_tenant_from_ec_topology,
		retire_ec_topology,
		type ECTopology,
		type TenantCandidate,
	} from '$lib/remote/replication.remote.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let topologyName = $derived(page.params.topology ?? '');

	let topologyData = $derived(topologyName ? get_ec_topology({ topologyName }) : undefined);
	let topology = $derived((topologyData?.current ?? null) as ECTopology | null);

	let candidatesData = $derived(
		topologyName ? get_ec_tenant_candidates({ topologyName }) : undefined
	);
	let candidates = $derived((candidatesData?.current ?? []) as TenantCandidate[]);

	// Add tenant dialog state
	let addTenantOpen = $state(false);
	let addTenantName = $state('');
	let addTenantError = $state('');
	let addTenantLoading = $state(false);

	// Remove tenant dialog state
	let removeTenantOpen = $state(false);
	let removeTenantName = $state('');

	// Retire state
	let retiring = $state(false);

	function stateVariant(state?: string): 'default' | 'secondary' | 'outline' {
		switch (state) {
			case 'ACTIVE':
				return 'default';
			case 'RETIRED':
				return 'secondary';
			default:
				return 'outline';
		}
	}

	function protectionVariant(status?: string): 'default' | 'destructive' | 'outline' {
		switch (status) {
			case 'PROTECTED':
				return 'default';
			case 'UNPROTECTED':
				return 'destructive';
			default:
				return 'outline';
		}
	}

	async function handleAddTenant(e: SubmitEvent) {
		e.preventDefault();
		if (!topologyData) return;
		addTenantLoading = true;
		addTenantError = '';
		try {
			await add_tenant_to_ec_topology({
				topologyName,
				tenantName: addTenantName,
			}).updates(topologyData);
			toast.success(`Tenant "${addTenantName}" added to topology`);
			addTenantOpen = false;
			addTenantName = '';
		} catch (err) {
			addTenantError = getErrorMessage(err, 'Failed to add tenant');
		} finally {
			addTenantLoading = false;
		}
	}

	async function handleRemoveTenant() {
		if (!topologyData) return;
		try {
			await remove_tenant_from_ec_topology({
				topologyName,
				tenantName: removeTenantName,
			}).updates(topologyData);
			toast.success(`Tenant "${removeTenantName}" removed from topology`);
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to remove tenant'));
		}
	}

	async function handleRetire() {
		if (!topologyData) return;
		retiring = true;
		try {
			await retire_ec_topology({ topologyName }).updates(topologyData);
			toast.success(`Topology "${topologyName}" retired`);
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to retire topology'));
		} finally {
			retiring = false;
		}
	}
</script>

<svelte:head>
	<title>{topologyName} - Erasure Coding - System - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<div class="flex items-center gap-4">
		<BackButton href="/system/erasure-coding" label="Back to erasure coding" />
		<PageHeader
			title={topologyName}
			description={topology
				? `Type: ${topology.type ?? '---'} | State: ${topology.state ?? '---'}`
				: 'Loading...'}
		/>
	</div>

	{#await topologyData}
		<CardSkeleton />
		<CardSkeleton />
	{:then}
		{#if !topology}
			<Card.Root>
				<Card.Content class="py-8 text-center text-sm text-muted-foreground">
					Topology not found or could not be loaded.
				</Card.Content>
			</Card.Root>
		{:else}
			<!-- Topology Info Card -->
			<Card.Root>
				<Card.Header>
					<Card.Title>Topology Information</Card.Title>
					<Card.Description>Configuration and status details</Card.Description>
				</Card.Header>
				<Card.Content>
					<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
						<div class="space-y-1">
							<p class="text-sm font-medium text-muted-foreground">Type</p>
							<p class="text-sm">{topology.type ?? '---'}</p>
						</div>
						<div class="space-y-1">
							<p class="text-sm font-medium text-muted-foreground">State</p>
							<Badge variant={stateVariant(topology.state)}>
								{topology.state ?? '---'}
							</Badge>
						</div>
						<div class="space-y-1">
							<p class="text-sm font-medium text-muted-foreground">Protection Status</p>
							<Badge variant={protectionVariant(topology.protectionStatus)}>
								{topology.protectionStatus ?? '---'}
							</Badge>
						</div>
						<div class="space-y-1">
							<p class="text-sm font-medium text-muted-foreground">Read Status</p>
							<p class="text-sm">{topology.readStatus ?? '---'}</p>
						</div>
						<div class="space-y-1">
							<p class="text-sm font-medium text-muted-foreground">Description</p>
							<p class="text-sm">{topology.description ?? '---'}</p>
						</div>
						<div class="space-y-1">
							<p class="text-sm font-medium text-muted-foreground">Erasure Coding Delay</p>
							<p class="text-sm">{topology.erasureCodingDelay ?? '---'}</p>
						</div>
						<div class="space-y-1">
							<p class="text-sm font-medium text-muted-foreground">Full Copy</p>
							<Badge variant={topology.fullCopy ? 'default' : 'secondary'}>
								{topology.fullCopy ? 'Enabled' : 'Disabled'}
							</Badge>
						</div>
						<div class="space-y-1">
							<p class="text-sm font-medium text-muted-foreground">Minimum Object Size</p>
							<p class="text-sm">
								{topology.minimumObjectSize != null
									? formatBytes(topology.minimumObjectSize)
									: '---'}
							</p>
						</div>
						<div class="space-y-1">
							<p class="text-sm font-medium text-muted-foreground">Restore Period</p>
							<p class="text-sm">{topology.restorePeriod ?? '---'}</p>
						</div>
						<div class="space-y-1">
							<p class="text-sm font-medium text-muted-foreground">EC Objects</p>
							<p class="text-sm font-mono">
								{(topology.erasureCodedObjects ?? 0).toLocaleString()}
							</p>
						</div>
					</div>
				</Card.Content>
			</Card.Root>

			<!-- Replication Links Card -->
			<Card.Root>
				<Card.Header>
					<Card.Title>Replication Links</Card.Title>
					<Card.Description>
						{(topology.replicationLinks ?? []).length} linked replication link{(
							topology.replicationLinks ?? []
						).length !== 1
							? 's'
							: ''}
					</Card.Description>
				</Card.Header>
				<Card.Content>
					{#if (topology.replicationLinks ?? []).length === 0}
						<p class="py-8 text-center text-sm text-muted-foreground">
							No replication links associated with this topology.
						</p>
					{:else}
						<Table.Root>
							<Table.Header>
								<Table.Row>
									<Table.Head>Name</Table.Head>
									<Table.Head>HCP Systems</Table.Head>
									<Table.Head>State</Table.Head>
									<Table.Head class="text-right">Paused Tenants</Table.Head>
								</Table.Row>
							</Table.Header>
							<Table.Body>
								{#each topology.replicationLinks ?? [] as link (link.name)}
									<Table.Row>
										<Table.Cell class="font-medium">{link.name ?? '---'}</Table.Cell>
										<Table.Cell>
											{(link.hcpSystems ?? []).join(', ') || '---'}
										</Table.Cell>
										<Table.Cell>
											<Badge variant={stateVariant(link.state)}>
												{link.state ?? '---'}
											</Badge>
										</Table.Cell>
										<Table.Cell class="text-right font-mono">
											{link.pausedTenantsCount ?? 0}
										</Table.Cell>
									</Table.Row>
								{/each}
							</Table.Body>
						</Table.Root>
					{/if}
				</Card.Content>
			</Card.Root>

			<!-- Tenants Card -->
			<Card.Root>
				<Card.Header>
					<div class="flex items-center justify-between">
						<div>
							<Card.Title>Tenants</Card.Title>
							<Card.Description>
								{(topology.tenants ?? []).length} tenant{(topology.tenants ?? []).length !== 1
									? 's'
									: ''} included
							</Card.Description>
						</div>
						<Button size="sm" onclick={() => (addTenantOpen = true)}>Add Tenant</Button>
					</div>
				</Card.Header>
				<Card.Content>
					{#if (topology.tenants ?? []).length === 0}
						<p class="py-8 text-center text-sm text-muted-foreground">
							No tenants included in this topology.
						</p>
					{:else}
						<div class="flex flex-wrap gap-2">
							{#each topology.tenants ?? [] as tenant (tenant)}
								<Badge variant="outline" class="gap-1 pr-1">
									{tenant}
									<button
										type="button"
										class="ml-1 rounded-full p-0.5 hover:bg-muted"
										onclick={() => {
											removeTenantName = tenant;
											removeTenantOpen = true;
										}}
										aria-label="Remove {tenant}"
									>
										<X class="h-3 w-3" />
									</button>
								</Badge>
							{/each}
						</div>
					{/if}
				</Card.Content>
			</Card.Root>

			<!-- Actions Card -->
			<Card.Root>
				<Card.Header>
					<Card.Title>Actions</Card.Title>
					<Card.Description>Administrative actions for this topology</Card.Description>
				</Card.Header>
				<Card.Content>
					<Button
						variant="destructive"
						disabled={retiring || topology.state === 'RETIRED'}
						onclick={handleRetire}
					>
						{retiring ? 'Retiring...' : 'Retire Topology'}
					</Button>
				</Card.Content>
			</Card.Root>
		{/if}
	{/await}
</div>

<FormDialog
	bind:open={addTenantOpen}
	title="Add Tenant"
	submitLabel="Add"
	loading={addTenantLoading}
	error={addTenantError}
	onsubmit={handleAddTenant}
>
	<div class="space-y-4">
		<div class="space-y-1.5">
			<Label for="add-tenant-name">Tenant</Label>
			{#if candidates.length > 0}
				<select
					id="add-tenant-name"
					class="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
					bind:value={addTenantName}
					required
				>
					<option value="" disabled>Select a tenant</option>
					{#each candidates as candidate (candidate.name)}
						<option value={candidate.name}>{candidate.name}</option>
					{/each}
				</select>
			{:else}
				<Input id="add-tenant-name" bind:value={addTenantName} required placeholder="Tenant name" />
			{/if}
		</div>
	</div>
</FormDialog>

<DeleteConfirmDialog
	bind:open={removeTenantOpen}
	name={removeTenantName}
	itemType="Tenant"
	description="Are you sure you want to remove this tenant from the erasure coding topology? The tenant will no longer participate in erasure coding."
	onconfirm={handleRemoveTenant}
/>
