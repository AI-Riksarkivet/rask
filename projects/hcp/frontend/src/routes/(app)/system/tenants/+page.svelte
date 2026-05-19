<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import * as Table from '$lib/components/ui/table/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import PageHeader from '$lib/components/custom/page-header/page-header.svelte';
	import FormDialog from '$lib/components/custom/form-dialog/form-dialog.svelte';
	import DeleteConfirmDialog from '$lib/components/custom/delete-confirm-dialog/delete-confirm-dialog.svelte';
	import { Plus } from 'lucide-svelte';
	import { toast } from 'svelte-sonner';
	import {
		get_all_tenants,
		create_tenant,
		delete_tenant,
		type TenantListEntry,
	} from '$lib/remote/system.remote.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let tenantsData = $derived(get_all_tenants({}));
	let tenants = $derived((tenantsData?.current ?? []) as TenantListEntry[]);

	let createOpen = $state(false);
	let createName = $state('');
	let createDescription = $state('');
	let createUsername = $state('');
	let createPassword = $state('');
	let createQuota = $state('10 GB');
	let createError = $state('');
	let createLoading = $state(false);

	let deleteOpen = $state(false);
	let deleteName = $state('');

	async function handleCreate(e: SubmitEvent) {
		e.preventDefault();
		createLoading = true;
		createError = '';
		try {
			await create_tenant({
				body: {
					name: createName,
					systemVisibleDescription: createDescription || undefined,
					hardQuota: createQuota,
				},
				username: createUsername,
				password: createPassword,
			}).updates(tenantsData);
			toast.success(`Tenant "${createName}" created`);
			createOpen = false;
			createName = '';
			createDescription = '';
			createUsername = '';
			createPassword = '';
		} catch (err) {
			createError = getErrorMessage(err, 'Failed to create tenant');
		} finally {
			createLoading = false;
		}
	}

	async function handleDelete() {
		try {
			await delete_tenant({ name: deleteName }).updates(tenantsData);
			toast.success(`Tenant "${deleteName}" deleted`);
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to delete tenant'));
		}
	}
</script>

<svelte:head>
	<title>Tenants - System - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<PageHeader title="Tenant Management" description="Create and manage HCP tenants">
		{#snippet actions()}
			<Button onclick={() => (createOpen = true)}>
				<Plus class="mr-1.5 h-4 w-4" />
				Create Tenant
			</Button>
		{/snippet}
	</PageHeader>

	{#await tenantsData}
		<CardSkeleton />
	{:then}
		<Card.Root>
			<Card.Header>
				<Card.Title>All Tenants</Card.Title>
				<Card.Description>
					{tenants.length} tenant{tenants.length !== 1 ? 's' : ''} in the system
				</Card.Description>
			</Card.Header>
			<Card.Content>
				{#if tenants.length === 0}
					<p class="py-8 text-center text-sm text-muted-foreground">No tenants found.</p>
				{:else}
					<Table.Root>
						<Table.Header>
							<Table.Row>
								<Table.Head>Name</Table.Head>
								<Table.Head>Description</Table.Head>
								<Table.Head>Hard Quota</Table.Head>
								<Table.Head>Soft Quota</Table.Head>
								<Table.Head>Namespace Quota</Table.Head>
								<Table.Head>Administration</Table.Head>
								<Table.Head class="w-[80px]"></Table.Head>
							</Table.Row>
						</Table.Header>
						<Table.Body>
							{#each tenants as t (t.name)}
								<Table.Row>
									<Table.Cell class="font-medium">{t.name ?? '—'}</Table.Cell>
									<Table.Cell class="max-w-[200px] truncate text-muted-foreground">
										{t.systemVisibleDescription ?? '—'}
									</Table.Cell>
									<Table.Cell>{t.hardQuota ?? '—'}</Table.Cell>
									<Table.Cell>{t.softQuota != null ? `${t.softQuota}%` : '—'}</Table.Cell>
									<Table.Cell>{t.namespaceQuota ?? '—'}</Table.Cell>
									<Table.Cell>
										{#if t.administrationAllowed}
											<Badge variant="default">Allowed</Badge>
										{:else}
											<Badge variant="secondary">Restricted</Badge>
										{/if}
									</Table.Cell>
									<Table.Cell>
										<Button
											variant="ghost"
											size="sm"
											class="text-destructive"
											onclick={() => {
												deleteName = t.name ?? '';
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
	title="Create Tenant"
	loading={createLoading}
	error={createError}
	onsubmit={handleCreate}
>
	<div class="space-y-4">
		<div class="space-y-1.5">
			<Label for="tenant-name">Tenant Name</Label>
			<Input id="tenant-name" bind:value={createName} required placeholder="my-tenant" />
		</div>
		<div class="space-y-1.5">
			<Label for="tenant-desc">Description</Label>
			<Input id="tenant-desc" bind:value={createDescription} placeholder="Optional description" />
		</div>
		<div class="space-y-1.5">
			<Label for="tenant-quota">Hard Quota</Label>
			<Input id="tenant-quota" bind:value={createQuota} placeholder="e.g. 100 GB" />
		</div>
		<hr />
		<p class="text-sm text-muted-foreground">Initial administrator account</p>
		<div class="space-y-1.5">
			<Label for="admin-user">Admin Username</Label>
			<Input id="admin-user" bind:value={createUsername} required placeholder="admin" />
		</div>
		<div class="space-y-1.5">
			<Label for="admin-pass">Admin Password</Label>
			<Input id="admin-pass" type="password" bind:value={createPassword} required />
		</div>
	</div>
</FormDialog>

<DeleteConfirmDialog
	bind:open={deleteOpen}
	name={deleteName}
	itemType="Tenant"
	description="This action cannot be undone and will delete all namespaces and data within the tenant."
	onconfirm={handleDelete}
/>
