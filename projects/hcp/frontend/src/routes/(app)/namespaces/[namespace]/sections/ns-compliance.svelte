<script lang="ts">
	import { Switch } from '$lib/components/ui/switch/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import * as Card from '$lib/components/ui/card/index.js';
	import SaveButton from '$lib/components/custom/save-button/save-button.svelte';
	import { useSave } from '$lib/utils/use-save.svelte.js';
	import {
		get_ns_compliance,
		update_ns_compliance,
		type ComplianceSettings,
	} from '$lib/remote/namespaces.remote.js';

	let {
		tenant,
		namespaceName,
	}: {
		tenant: string;
		namespaceName: string;
	} = $props();

	let complianceData = $derived(get_ns_compliance({ tenant, name: namespaceName }));
	let compliance = $derived((complianceData?.current ?? {}) as ComplianceSettings);

	const saver = useSave({
		successMsg: 'Compliance settings updated',
		errorMsg: 'Failed to update compliance settings',
	});

	let localRetentionDefault = $state('');
	let localMinRetention = $state('');
	let localShreddingDefault = $state(false);
	let localCustomMetadataChanges = $state('');
	let localDispositionEnabled = $state(false);

	$effect(() => {
		const c = compliance;
		void saver.syncVersion;
		localRetentionDefault = c.retentionDefault ?? '';
		localMinRetention = c.minimumRetentionAfterInitialUnspecified ?? '';
		localShreddingDefault = c.shreddingDefault ?? false;
		localCustomMetadataChanges = c.customMetadataChanges ?? '';
		localDispositionEnabled = c.dispositionEnabled ?? false;
	});

	let dirty = $derived(
		localRetentionDefault !== (compliance.retentionDefault ?? '') ||
			localMinRetention !== (compliance.minimumRetentionAfterInitialUnspecified ?? '') ||
			localShreddingDefault !== (compliance.shreddingDefault ?? false) ||
			localCustomMetadataChanges !== (compliance.customMetadataChanges ?? '') ||
			localDispositionEnabled !== (compliance.dispositionEnabled ?? false)
	);
</script>

<Card.Root class="flex h-full flex-col">
	<Card.Header>
		<Card.Title>Compliance</Card.Title>
		<Card.Description>
			Controls how objects are retained and protected from modification or deletion.
		</Card.Description>
	</Card.Header>
	{#await complianceData}
		<Card.Content class="flex-1">
			<div class="flex flex-wrap gap-6">
				{#each Array(5) as _, i (i)}
					<div class="h-5 w-32 animate-pulse rounded bg-muted"></div>
				{/each}
			</div>
		</Card.Content>
	{:then}
		<Card.Content class="flex-1 space-y-4">
			<div class="grid gap-4 sm:grid-cols-2">
				<div class="space-y-1.5">
					<Label for="retention-default">Retention Default</Label>
					<Input
						id="retention-default"
						placeholder="e.g. 0, A+1y, or -1"
						bind:value={localRetentionDefault}
					/>
					<p class="text-xs text-muted-foreground">
						0 = deletion allowed, -1 = indefinite, offsets like A+1y.
					</p>
				</div>
				<div class="space-y-1.5">
					<Label for="min-retention">Min Retention After Initial</Label>
					<Input id="min-retention" placeholder="e.g. 0 or A+90d" bind:value={localMinRetention} />
					<p class="text-xs text-muted-foreground">
						Floor for objects initially stored without retention.
					</p>
				</div>
			</div>
			<div class="space-y-1.5">
				<Label for="custom-metadata-changes">Custom Metadata Changes</Label>
				<select
					id="custom-metadata-changes"
					class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-9 w-full items-center rounded-md border px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
					bind:value={localCustomMetadataChanges}
				>
					<option value="" disabled>Select...</option>
					<option value="allowed">Allowed</option>
					<option value="notAllowed">Not Allowed</option>
					<option value="creationOnly">Creation Only</option>
				</select>
			</div>
			<div class="flex flex-wrap gap-x-6 gap-y-3">
				<div class="flex items-center gap-2">
					<Switch id="shredding-default" bind:checked={localShreddingDefault} />
					<Label for="shredding-default" class="text-sm">Shredding Default</Label>
				</div>
				<div class="flex items-center gap-2">
					<Switch id="disposition-enabled" bind:checked={localDispositionEnabled} />
					<Label for="disposition-enabled" class="text-sm">Disposition</Label>
				</div>
			</div>
		</Card.Content>
		<Card.Footer>
			<SaveButton
				{dirty}
				saving={saver.saving}
				onclick={() =>
					saver.run(async () => {
						if (!complianceData) return;
						await update_ns_compliance({
							tenant,
							name: namespaceName,
							body: {
								retentionDefault: localRetentionDefault || undefined,
								minimumRetentionAfterInitialUnspecified: localMinRetention || undefined,
								shreddingDefault: localShreddingDefault,
								customMetadataChanges: localCustomMetadataChanges || undefined,
								dispositionEnabled: localDispositionEnabled,
							},
						}).updates(complianceData);
					})}
			/>
		</Card.Footer>
	{/await}
</Card.Root>
