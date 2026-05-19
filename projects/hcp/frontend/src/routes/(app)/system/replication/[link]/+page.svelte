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
	import { X } from 'lucide-svelte';
	import { toast } from 'svelte-sonner';
	import { formatBytes, formatDate, formatNumber } from '$lib/utils/format.js';
	import {
		get_replication_link,
		get_link_content,
		get_link_schedule,
		action_replication_link,
		add_tenant_to_link,
		remove_tenant_from_link,
		type ReplicationLink,
		type LinkContent,
		type LinkSchedule,
	} from '$lib/remote/replication.remote.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let linkName = $derived(page.params.link);

	// ── Data queries ───────────────────────────────────────────────
	let linkData = $derived(linkName ? get_replication_link({ linkName }) : undefined);
	let link = $derived((linkData?.current ?? {}) as ReplicationLink);

	let contentData = $derived(linkName ? get_link_content({ linkName }) : undefined);
	let content = $derived((contentData?.current ?? {}) as LinkContent);

	let scheduleData = $derived(linkName ? get_link_schedule({ linkName }) : undefined);
	let schedule = $derived((scheduleData?.current ?? {}) as LinkSchedule);

	// ── Actions ────────────────────────────────────────────────────
	let actionLoading = $state('');

	type LinkAction =
		| 'suspend'
		| 'resume'
		| 'failOver'
		| 'failBack'
		| 'beginRecover'
		| 'completeRecovery';

	async function handleAction(action: LinkAction, label: string) {
		if (!linkName || !linkData || actionLoading) return;
		actionLoading = action;
		try {
			await action_replication_link({ linkName, action }).updates(linkData);
			toast.success(`${label} completed successfully`);
		} catch (err) {
			toast.error(getErrorMessage(err, `Failed to ${label.toLowerCase()}`));
		} finally {
			actionLoading = '';
		}
	}

	// ── Add Tenant Dialog ──────────────────────────────────────────
	let addTenantOpen = $state(false);
	let addTenantName = $state('');
	let addTenantLoading = $state(false);
	let addTenantError = $state('');

	async function handleAddTenant(e: SubmitEvent) {
		e.preventDefault();
		if (!linkName || !contentData || !addTenantName.trim()) return;
		addTenantLoading = true;
		addTenantError = '';
		try {
			await add_tenant_to_link({ linkName, tenantName: addTenantName.trim() }).updates(contentData);
			toast.success(`Tenant "${addTenantName.trim()}" added to link`);
			addTenantName = '';
			addTenantOpen = false;
		} catch (err) {
			addTenantError = getErrorMessage(err, 'Failed to add tenant to link');
		} finally {
			addTenantLoading = false;
		}
	}

	// ── Remove Tenant ──────────────────────────────────────────────
	let removingTenant = $state('');

	async function handleRemoveTenant(tenantName: string) {
		if (!linkName || !contentData || removingTenant) return;
		removingTenant = tenantName;
		try {
			await remove_tenant_from_link({ linkName, tenantName }).updates(contentData);
			toast.success(`Tenant "${tenantName}" removed from link`);
		} catch (err) {
			toast.error(getErrorMessage(err, `Failed to remove tenant "${tenantName}"`));
		} finally {
			removingTenant = '';
		}
	}
</script>

<svelte:head>
	<title>{linkName} - Replication - System - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<!-- Header -->
	<div class="flex items-center gap-4">
		<BackButton href="/system/replication" label="Back to replication links" />
		<PageHeader
			title={linkName ?? ''}
			description={link.type && link.status
				? `${link.type} link — ${link.status}${link.suspended ? ' (Suspended)' : ''}`
				: 'Replication link details'}
		/>
	</div>

	{#await linkData}
		<div class="grid gap-6 lg:grid-cols-2">
			<CardSkeleton />
			<CardSkeleton />
			<CardSkeleton />
			<CardSkeleton />
		</div>
	{:then}
		<!-- Link Info Card -->
		<Card.Root>
			<Card.Header>
				<Card.Title>Link Information</Card.Title>
				<Card.Description>Connection details and configuration</Card.Description>
			</Card.Header>
			<Card.Content>
				<dl class="grid grid-cols-[auto_1fr] gap-x-6 gap-y-3 text-sm">
					<dt class="text-muted-foreground">Remote Host</dt>
					<dd class="font-mono">
						{link.connection?.remoteHost ?? '—'}:{link.connection?.remotePort ?? '—'}
					</dd>

					<dt class="text-muted-foreground">Local Host</dt>
					<dd class="font-mono">
						{link.connection?.localHost ?? '—'}:{link.connection?.localPort ?? '—'}
					</dd>

					<dt class="text-muted-foreground">Compression</dt>
					<dd>
						<Badge variant={link.compression ? 'default' : 'secondary'}>
							{link.compression ? 'Enabled' : 'Disabled'}
						</Badge>
					</dd>

					<dt class="text-muted-foreground">Encryption</dt>
					<dd>
						<Badge variant={link.encryption ? 'default' : 'secondary'}>
							{link.encryption ? 'Enabled' : 'Disabled'}
						</Badge>
					</dd>

					<dt class="text-muted-foreground">Priority</dt>
					<dd>{link.priority ?? '—'}</dd>

					<dt class="text-muted-foreground">Description</dt>
					<dd>{link.description || '—'}</dd>
				</dl>
			</Card.Content>
		</Card.Root>

		<!-- Statistics Card -->
		<Card.Root>
			<Card.Header>
				<Card.Title>Replication Statistics</Card.Title>
				<Card.Description>Current replication throughput and status</Card.Description>
			</Card.Header>
			<Card.Content>
				<dl
					class="grid grid-cols-[auto_1fr] gap-x-6 gap-y-3 text-sm sm:grid-cols-[auto_1fr_auto_1fr]"
				>
					<dt class="text-muted-foreground">Bytes Replicated</dt>
					<dd>
						{link.statistics?.bytesReplicated != null
							? formatBytes(link.statistics.bytesReplicated)
							: '—'}
					</dd>

					<dt class="text-muted-foreground">Bytes Pending</dt>
					<dd>
						{link.statistics?.bytesPending != null
							? formatBytes(link.statistics.bytesPending)
							: '—'}
					</dd>

					<dt class="text-muted-foreground">Objects Replicated</dt>
					<dd>{formatNumber(link.statistics?.objectsReplicated)}</dd>

					<dt class="text-muted-foreground">Objects Pending</dt>
					<dd>{formatNumber(link.statistics?.objectsPending)}</dd>

					<dt class="text-muted-foreground">Operations/sec</dt>
					<dd>
						{link.statistics?.operationsPerSecond != null
							? `${formatNumber(link.statistics.operationsPerSecond)}/s`
							: '—'}
					</dd>

					<dt class="text-muted-foreground">Bytes/sec</dt>
					<dd>
						{link.statistics?.bytesPerSecond != null
							? formatBytes(link.statistics.bytesPerSecond) + '/s'
							: '—'}
					</dd>

					<dt class="text-muted-foreground">Errors</dt>
					<dd>
						{#if link.statistics?.errors != null && link.statistics.errors > 0}
							<Badge variant="destructive">{formatNumber(link.statistics.errors)}</Badge>
						{:else}
							{formatNumber(link.statistics?.errors)}
						{/if}
					</dd>

					<dt class="text-muted-foreground">Up to Date As Of</dt>
					<dd>
						{#if link.statistics?.upToDateAsOfString}
							{formatDate(link.statistics.upToDateAsOfString)}
						{:else if link.statistics?.upToDateAsOfMillis}
							{formatDate(new Date(link.statistics.upToDateAsOfMillis))}
						{:else}
							—
						{/if}
					</dd>
				</dl>
			</Card.Content>
		</Card.Root>

		<!-- Actions Card -->
		<Card.Root>
			<Card.Header>
				<Card.Title>Link Actions</Card.Title>
				<Card.Description>Manage the replication link state</Card.Description>
			</Card.Header>
			<Card.Content>
				<div class="flex flex-wrap gap-3">
					{#if link.suspended}
						<Button
							variant="outline"
							disabled={actionLoading !== ''}
							onclick={() => handleAction('resume', 'Resume')}
						>
							{actionLoading === 'resume' ? 'Resuming...' : 'Resume'}
						</Button>
					{:else}
						<Button
							variant="outline"
							disabled={actionLoading !== ''}
							onclick={() => handleAction('suspend', 'Suspend')}
						>
							{actionLoading === 'suspend' ? 'Suspending...' : 'Suspend'}
						</Button>
					{/if}

					<Button
						variant="destructive"
						disabled={actionLoading !== ''}
						onclick={() => handleAction('failOver', 'Fail Over')}
					>
						{actionLoading === 'failOver' ? 'Failing over...' : 'Fail Over'}
					</Button>

					<Button
						variant="destructive"
						disabled={actionLoading !== ''}
						onclick={() => handleAction('failBack', 'Fail Back')}
					>
						{actionLoading === 'failBack' ? 'Failing back...' : 'Fail Back'}
					</Button>

					<Button
						variant="secondary"
						disabled={actionLoading !== ''}
						onclick={() => handleAction('beginRecover', 'Begin Recovery')}
					>
						{actionLoading === 'beginRecover' ? 'Starting...' : 'Begin Recovery'}
					</Button>

					<Button
						variant="secondary"
						disabled={actionLoading !== ''}
						onclick={() => handleAction('completeRecovery', 'Complete Recovery')}
					>
						{actionLoading === 'completeRecovery' ? 'Completing...' : 'Complete Recovery'}
					</Button>
				</div>
			</Card.Content>
		</Card.Root>

		<!-- Content Card -->
		{#await contentData}
			<CardSkeleton />
		{:then}
			<Card.Root>
				<Card.Header>
					<div class="flex items-center justify-between">
						<div>
							<Card.Title>Link Content</Card.Title>
							<Card.Description>Tenants, directories, and chained links</Card.Description>
						</div>
						<Button variant="outline" size="sm" onclick={() => (addTenantOpen = true)}>
							Add Tenant
						</Button>
					</div>
				</Card.Header>
				<Card.Content class="space-y-4">
					<!-- Tenants -->
					<div>
						<p class="mb-2 text-sm font-medium text-muted-foreground">Tenants</p>
						{#if content.tenants && content.tenants.length > 0}
							<div class="flex flex-wrap gap-2">
								{#each content.tenants as tenant (tenant)}
									<Badge variant="outline" class="gap-1 pr-1">
										{tenant}
										<button
											type="button"
											class="ml-1 rounded-full p-0.5 hover:bg-muted"
											disabled={removingTenant !== ''}
											onclick={() => handleRemoveTenant(tenant)}
											aria-label="Remove {tenant}"
										>
											<X class="h-3 w-3" />
										</button>
									</Badge>
								{/each}
							</div>
						{:else}
							<p class="text-sm text-muted-foreground">No tenants assigned</p>
						{/if}
					</div>

					<!-- Directories -->
					<div>
						<p class="mb-2 text-sm font-medium text-muted-foreground">Directories</p>
						{#if content.defaultNamespaceDirectories && content.defaultNamespaceDirectories.length > 0}
							<div class="flex flex-wrap gap-2">
								{#each content.defaultNamespaceDirectories as dir (dir)}
									<Badge variant="secondary">{dir}</Badge>
								{/each}
							</div>
						{:else}
							<p class="text-sm text-muted-foreground">No directories</p>
						{/if}
					</div>

					<!-- Chained Links -->
					<div>
						<p class="mb-2 text-sm font-medium text-muted-foreground">Chained Links</p>
						{#if content.chainedLinks && content.chainedLinks.length > 0}
							<div class="flex flex-wrap gap-2">
								{#each content.chainedLinks as chain (chain)}
									<Badge variant="secondary">{chain}</Badge>
								{/each}
							</div>
						{:else}
							<p class="text-sm text-muted-foreground">No chained links</p>
						{/if}
					</div>
				</Card.Content>
			</Card.Root>
		{/await}

		<!-- Schedule Card -->
		{#await scheduleData}
			<CardSkeleton />
		{:then}
			<Card.Root>
				<Card.Header>
					<Card.Title>Replication Schedule</Card.Title>
					<Card.Description>Local and remote schedule transitions</Card.Description>
				</Card.Header>
				<Card.Content class="space-y-6">
					<!-- Local Schedule -->
					<div>
						<h4 class="mb-2 text-sm font-medium">Local Schedule</h4>
						{#if schedule.local?.transition && schedule.local.transition.length > 0}
							<Table.Root>
								<Table.Header>
									<Table.Row>
										<Table.Head>Time</Table.Head>
										<Table.Head>Performance Level</Table.Head>
									</Table.Row>
								</Table.Header>
								<Table.Body>
									{#each schedule.local.transition as transition, i (i)}
										<Table.Row>
											<Table.Cell class="font-mono">
												{transition.time ?? '—'}
											</Table.Cell>
											<Table.Cell>
												<Badge variant="outline">
													{transition.performanceLevel ?? '—'}
												</Badge>
											</Table.Cell>
										</Table.Row>
									{/each}
								</Table.Body>
							</Table.Root>
						{:else}
							<p class="text-sm text-muted-foreground">No local schedule transitions</p>
						{/if}
					</div>

					<!-- Remote Schedule -->
					<div>
						<h4 class="mb-2 text-sm font-medium">Remote Schedule</h4>
						{#if schedule.remote?.transition && schedule.remote.transition.length > 0}
							<Table.Root>
								<Table.Header>
									<Table.Row>
										<Table.Head>Time</Table.Head>
										<Table.Head>Performance Level</Table.Head>
									</Table.Row>
								</Table.Header>
								<Table.Body>
									{#each schedule.remote.transition as transition, i (i)}
										<Table.Row>
											<Table.Cell class="font-mono">
												{transition.time ?? '—'}
											</Table.Cell>
											<Table.Cell>
												<Badge variant="outline">
													{transition.performanceLevel ?? '—'}
												</Badge>
											</Table.Cell>
										</Table.Row>
									{/each}
								</Table.Body>
							</Table.Root>
						{:else}
							<p class="text-sm text-muted-foreground">No remote schedule transitions</p>
						{/if}
					</div>
				</Card.Content>
			</Card.Root>
		{/await}
	{/await}
</div>

<!-- Add Tenant Dialog -->
<FormDialog
	bind:open={addTenantOpen}
	title="Add Tenant to Link"
	loading={addTenantLoading}
	error={addTenantError}
	onsubmit={handleAddTenant}
>
	<div class="space-y-2">
		<Label for="tenant-name">Tenant Name</Label>
		<Input id="tenant-name" placeholder="Enter tenant name" bind:value={addTenantName} required />
	</div>
</FormDialog>
