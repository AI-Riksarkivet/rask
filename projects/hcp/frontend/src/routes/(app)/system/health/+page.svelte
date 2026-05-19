<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Switch } from '$lib/components/ui/switch/index.js';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import PageHeader from '$lib/components/custom/page-header/page-header.svelte';
	import { toast } from 'svelte-sonner';
	import {
		get_health_status,
		prepare_health_report,
		cancel_health_report,
		type HealthStatus,
	} from '$lib/remote/system.remote.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let statusData = $derived(get_health_status({}));
	let status = $derived((statusData?.current ?? {}) as HealthStatus);

	let prepareLoading = $state(false);
	let startDate = $state('');
	let endDate = $state('');
	let collectCurrent = $state(true);

	async function handlePrepare() {
		prepareLoading = true;
		try {
			await prepare_health_report({
				startDate: startDate || undefined,
				endDate: endDate || undefined,
				collectCurrent,
			}).updates(statusData);
			toast.success('Health report preparation started');
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to prepare report'));
		} finally {
			prepareLoading = false;
		}
	}

	async function handleCancel() {
		try {
			await cancel_health_report({}).updates(statusData);
			toast.success('Health report cancelled');
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to cancel'));
		}
	}
</script>

<svelte:head>
	<title>Health - System - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<PageHeader
		title="Health Check Reports"
		description="Prepare and download system health check reports for diagnostics"
	/>

	{#await statusData}
		<div class="space-y-6">
			<CardSkeleton />
			<CardSkeleton />
		</div>
	{:then}
		<!-- Status -->
		<Card.Root class="max-w-2xl">
			<Card.Header>
				<Card.Title>Report Status</Card.Title>
				<Card.Description>Current state of health check report preparation.</Card.Description>
			</Card.Header>
			<Card.Content>
				<dl class="grid grid-cols-[auto_1fr] gap-x-6 gap-y-3 text-sm">
					<dt class="text-muted-foreground">Started</dt>
					<dd>
						{#if status.started}
							<Badge variant="default">Yes</Badge>
						{:else}
							<Badge variant="secondary">No</Badge>
						{/if}
					</dd>

					<dt class="text-muted-foreground">Ready for Download</dt>
					<dd>
						{#if status.readyForStreaming}
							<Badge variant="default">Ready</Badge>
						{:else}
							<Badge variant="secondary">Not Ready</Badge>
						{/if}
					</dd>

					<dt class="text-muted-foreground">Streaming</dt>
					<dd>
						{#if status.streamingInProgress}
							<Badge variant="default">In Progress</Badge>
						{:else}
							<Badge variant="secondary">Idle</Badge>
						{/if}
					</dd>

					<dt class="text-muted-foreground">Error</dt>
					<dd>
						{#if status.error}
							<Badge variant="destructive">Error</Badge>
						{:else}
							<Badge variant="secondary">None</Badge>
						{/if}
					</dd>
				</dl>

				{#if status.started}
					<div class="mt-4">
						<Button variant="outline" size="sm" onclick={handleCancel}>Cancel Report</Button>
					</div>
				{/if}
			</Card.Content>
		</Card.Root>

		<!-- Prepare -->
		<Card.Root class="max-w-2xl">
			<Card.Header>
				<Card.Title>Prepare Health Report</Card.Title>
				<Card.Description>Configure and prepare a new health check report.</Card.Description>
			</Card.Header>
			<Card.Content class="space-y-4">
				<div class="grid gap-4 sm:grid-cols-2">
					<div class="space-y-1.5">
						<Label for="health-start">Start Date</Label>
						<Input id="health-start" type="date" bind:value={startDate} />
					</div>
					<div class="space-y-1.5">
						<Label for="health-end">End Date</Label>
						<Input id="health-end" type="date" bind:value={endDate} />
					</div>
				</div>
				<div class="flex items-center gap-2">
					<Switch id="collect-current" bind:checked={collectCurrent} />
					<Label for="collect-current" class="text-sm">Collect current data</Label>
				</div>
				<p class="text-xs text-muted-foreground">
					When enabled, the report will include current system state along with historical data.
				</p>
			</Card.Content>
			<Card.Footer>
				<Button onclick={handlePrepare} disabled={prepareLoading}>
					{prepareLoading ? 'Preparing...' : 'Prepare Report'}
				</Button>
			</Card.Footer>
		</Card.Root>
	{/await}
</div>
