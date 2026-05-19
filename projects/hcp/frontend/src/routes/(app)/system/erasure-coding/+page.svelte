<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import * as Table from '$lib/components/ui/table/index.js';

	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Checkbox } from '$lib/components/ui/checkbox/index.js';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import PageHeader from '$lib/components/custom/page-header/page-header.svelte';
	import FormDialog from '$lib/components/custom/form-dialog/form-dialog.svelte';
	import DeleteConfirmDialog from '$lib/components/custom/delete-confirm-dialog/delete-confirm-dialog.svelte';
	import { Plus } from 'lucide-svelte';
	import { toast } from 'svelte-sonner';
	import {
		get_ec_topologies,
		create_ec_topology,
		delete_ec_topology,
		type ECTopology,
	} from '$lib/remote/replication.remote.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let topologiesData = $derived(get_ec_topologies({}));
	let topologies = $derived((topologiesData?.current ?? []) as ECTopology[]);

	// Create dialog state
	let createOpen = $state(false);
	let createName = $state('');
	let createType = $state('2+1');
	let createDescription = $state('');
	let createErasureCodingDelay = $state('');
	let createFullCopy = $state(false);
	let createMinimumObjectSize = $state('4096');
	let createRestorePeriod = $state('');
	let createError = $state('');
	let createLoading = $state(false);

	// Delete dialog state
	let deleteOpen = $state(false);
	let deleteName = $state('');

	async function handleCreate(e: SubmitEvent) {
		e.preventDefault();
		createLoading = true;
		createError = '';
		try {
			await create_ec_topology({
				body: {
					name: createName,
					type: createType,
					description: createDescription || undefined,
					erasureCodingDelay: createErasureCodingDelay
						? Number(createErasureCodingDelay)
						: undefined,
					fullCopy: createFullCopy,
					minimumObjectSize: createMinimumObjectSize ? Number(createMinimumObjectSize) : undefined,
					restorePeriod: createRestorePeriod ? Number(createRestorePeriod) : undefined,
				},
			}).updates(topologiesData);
			toast.success(`Topology "${createName}" created`);
			createOpen = false;
			createName = '';
			createType = '2+1';
			createDescription = '';
			createErasureCodingDelay = '';
			createFullCopy = false;
			createMinimumObjectSize = '4096';
			createRestorePeriod = '';
		} catch (err) {
			createError = getErrorMessage(err, 'Failed to create topology');
		} finally {
			createLoading = false;
		}
	}

	async function handleDelete() {
		try {
			await delete_ec_topology({ topologyName: deleteName }).updates(topologiesData);
			toast.success(`Topology "${deleteName}" deleted`);
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to delete topology'));
		}
	}

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
</script>

<svelte:head>
	<title>Erasure Coding - System - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<PageHeader
		title="Erasure Coding"
		description="Manage erasure coding topologies for storage efficiency"
	>
		{#snippet actions()}
			<Button onclick={() => (createOpen = true)}>
				<Plus class="mr-1.5 h-4 w-4" />
				Create Topology
			</Button>
		{/snippet}
	</PageHeader>

	{#await topologiesData}
		<CardSkeleton />
	{:then}
		<Card.Root>
			<Card.Header>
				<Card.Title>EC Topologies</Card.Title>
				<Card.Description>
					{topologies.length} topology{topologies.length !== 1 ? 'ies' : 'y'} configured
				</Card.Description>
			</Card.Header>
			<Card.Content>
				{#if topologies.length === 0}
					<p class="py-8 text-center text-sm text-muted-foreground">
						No erasure coding topologies found.
					</p>
				{:else}
					<Table.Root>
						<Table.Header>
							<Table.Row>
								<Table.Head>Name</Table.Head>
								<Table.Head>Type</Table.Head>
								<Table.Head>State</Table.Head>
								<Table.Head>Protection Status</Table.Head>
								<Table.Head class="text-right">EC Objects</Table.Head>
								<Table.Head>HCP Systems</Table.Head>
								<Table.Head class="w-[80px]"></Table.Head>
							</Table.Row>
						</Table.Header>
						<Table.Body>
							{#each topologies as topology (topology.name)}
								<Table.Row>
									<Table.Cell class="font-medium">
										<a
											href="/system/erasure-coding/{topology.name}"
											class="text-primary underline-offset-4 hover:underline"
										>
											{topology.name ?? '---'}
										</a>
									</Table.Cell>
									<Table.Cell>
										<Badge variant="outline">{topology.type ?? '---'}</Badge>
									</Table.Cell>
									<Table.Cell>
										<Badge variant={stateVariant(topology.state)}>
											{topology.state ?? '---'}
										</Badge>
									</Table.Cell>
									<Table.Cell>
										<Badge variant={protectionVariant(topology.protectionStatus)}>
											{topology.protectionStatus ?? '---'}
										</Badge>
									</Table.Cell>
									<Table.Cell class="text-right font-mono">
										{(topology.erasureCodedObjects ?? 0).toLocaleString()}
									</Table.Cell>
									<Table.Cell>
										{(topology.hcpSystems ?? []).join(', ') || '---'}
									</Table.Cell>
									<Table.Cell>
										<Button
											variant="ghost"
											size="sm"
											class="text-destructive"
											onclick={() => {
												deleteName = topology.name ?? '';
												deleteOpen = true;
											}}
										>
											Delete
										</Button>
									</Table.Cell>
								</Table.Row>
							{/each}
						</Table.Body>
					</Table.Root>
				{/if}
			</Card.Content>
		</Card.Root>
	{/await}
</div>

<FormDialog
	bind:open={createOpen}
	title="Create Topology"
	loading={createLoading}
	error={createError}
	onsubmit={handleCreate}
>
	<div class="space-y-4">
		<div class="space-y-1.5">
			<Label for="ec-name">Name</Label>
			<Input id="ec-name" bind:value={createName} required placeholder="my-ec-topology" />
		</div>
		<div class="space-y-1.5">
			<Label for="ec-type">Type</Label>
			<select
				id="ec-type"
				class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-9 w-full items-center rounded-md border px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
				bind:value={createType}
			>
				<option value="2+1">2+1</option>
				<option value="4+2">4+2</option>
				<option value="6+3">6+3</option>
			</select>
		</div>
		<div class="space-y-1.5">
			<Label for="ec-desc">Description</Label>
			<Input id="ec-desc" bind:value={createDescription} placeholder="Optional description" />
		</div>
		<div class="space-y-1.5">
			<Label for="ec-delay">Erasure Coding Delay</Label>
			<Input
				id="ec-delay"
				type="number"
				bind:value={createErasureCodingDelay}
				placeholder="Delay in seconds"
			/>
		</div>
		<div class="flex items-center gap-2">
			<Checkbox id="ec-fullcopy" bind:checked={createFullCopy} />
			<Label for="ec-fullcopy" class="text-sm">Full Copy</Label>
		</div>
		<div class="space-y-1.5">
			<Label for="ec-minsize">Minimum Object Size</Label>
			<Input
				id="ec-minsize"
				type="number"
				bind:value={createMinimumObjectSize}
				placeholder="4096"
			/>
		</div>
		<div class="space-y-1.5">
			<Label for="ec-restore">Restore Period</Label>
			<Input
				id="ec-restore"
				type="number"
				bind:value={createRestorePeriod}
				placeholder="Restore period in seconds"
			/>
		</div>
	</div>
</FormDialog>

<DeleteConfirmDialog
	bind:open={deleteOpen}
	name={deleteName}
	itemType="Topology"
	description="This action cannot be undone. The erasure coding topology and its configuration will be permanently removed."
	onconfirm={handleDelete}
/>
