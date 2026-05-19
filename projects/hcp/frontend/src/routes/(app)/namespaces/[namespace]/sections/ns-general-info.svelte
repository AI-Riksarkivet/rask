<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
	import { HelpCircle, Pencil, Check, X } from 'lucide-svelte';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import StorageProgressBar from '$lib/components/custom/storage-progress-bar/storage-progress-bar.svelte';
	import { formatDate, formatBytes, parseQuotaBytes, getStorageUsed } from '$lib/utils/format.js';
	import type { ChargebackEntry } from '$lib/utils/format.js';
	import { get_tenant_chargeback } from '$lib/remote/tenant-info.remote.js';
	import { toast } from 'svelte-sonner';
	import {
		get_namespace,
		get_ns_statistics,
		update_namespace,
		type Namespace,
		type NsStatistics,
	} from '$lib/remote/namespaces.remote.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let {
		tenant,
		namespaceName,
	}: {
		tenant: string;
		namespaceName: string;
	} = $props();

	let nsData = $derived(get_namespace({ tenant, name: namespaceName }));
	let ns = $derived((nsData?.current ?? null) as Namespace | null);

	let statsData = $derived(get_ns_statistics({ tenant, name: namespaceName }));
	let stats = $derived((statsData?.current ?? null) as NsStatistics | null);

	let chargebackData = $derived(get_tenant_chargeback({ tenant }));
	let nsStorageUsed = $derived(
		getStorageUsed(
			(chargebackData?.current?.chargebackData ?? []) as ChargebackEntry[],
			namespaceName
		)
	);

	// Owner editing
	let editingOwner = $state(false);
	let ownerInput = $state('');
	let savingOwner = $state(false);

	function startEditOwner() {
		ownerInput = ns?.owner ?? '';
		editingOwner = true;
	}

	function cancelEditOwner() {
		editingOwner = false;
	}

	async function saveOwner() {
		if (!nsData) return;
		savingOwner = true;
		try {
			await update_namespace({
				tenant,
				name: namespaceName,
				body: {
					owner: ownerInput || '',
					ownerType: ownerInput ? 'LOCAL' : '',
				},
			}).updates(nsData);
			toast.success('Namespace owner updated');
			editingOwner = false;
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to update owner'));
		} finally {
			savingOwner = false;
		}
	}

	// Hard quota editing
	let editingHardQuota = $state(false);
	let hardQuotaInput = $state('');
	let savingHardQuota = $state(false);

	function startEditHardQuota() {
		hardQuotaInput = ns?.hardQuota ?? '';
		editingHardQuota = true;
	}

	function cancelEditHardQuota() {
		editingHardQuota = false;
	}

	async function saveHardQuota() {
		if (!nsData) return;
		const trimmed = hardQuotaInput.trim();
		if (trimmed && !parseQuotaBytes(trimmed)) {
			toast.error('Invalid quota format. Use e.g. "50 GB" or "1.5 TB"');
			return;
		}
		savingHardQuota = true;
		try {
			await update_namespace({
				tenant,
				name: namespaceName,
				body: { hardQuota: trimmed },
			}).updates(nsData);
			toast.success('Hard quota updated');
			editingHardQuota = false;
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to update hard quota'));
		} finally {
			savingHardQuota = false;
		}
	}

	// Soft quota editing
	let editingSoftQuota = $state(false);
	let softQuotaInput = $state('');
	let savingSoftQuota = $state(false);

	function startEditSoftQuota() {
		softQuotaInput = ns?.softQuota != null ? String(ns.softQuota) : '';
		editingSoftQuota = true;
	}

	function cancelEditSoftQuota() {
		editingSoftQuota = false;
	}

	async function saveSoftQuota() {
		if (!nsData) return;
		const val = Number(softQuotaInput);
		if (softQuotaInput.trim() !== '' && (isNaN(val) || val < 10 || val > 95)) {
			toast.error('Soft quota must be an integer between 10 and 95');
			return;
		}
		savingSoftQuota = true;
		try {
			await update_namespace({
				tenant,
				name: namespaceName,
				body: { softQuota: softQuotaInput.trim() === '' ? null : val },
			}).updates(nsData);
			toast.success('Soft quota updated');
			editingSoftQuota = false;
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to update soft quota'));
		} finally {
			savingSoftQuota = false;
		}
	}

	const FIELD_DESCRIPTIONS: Record<string, string> = {
		description: 'A human-readable summary of the namespace purpose.',
		owner:
			'The HCP user account that owns this namespace. The owner sees this namespace as an S3 bucket.',
		hashScheme: 'The algorithm used to generate object content hashes (e.g. SHA-256, MD5).',
		created: 'The date and time the namespace was created.',
		storageUsed:
			'Total logical storage consumed by objects in this namespace, derived from chargeback data.',
		hardQuota:
			'The maximum amount of storage this namespace is allowed to consume. Empty means unlimited.',
		softQuota:
			'A percentage of the hard quota at which warning notifications are triggered (0–100%).',
		objects: 'Total number of objects currently stored in this namespace.',
	};
</script>

{#snippet fieldLabel(label: string, descKey: string)}
	<p
		class="flex items-center gap-1 text-xs font-medium uppercase tracking-wide text-muted-foreground"
	>
		{label}
		{#if FIELD_DESCRIPTIONS[descKey]}
			<Tooltip.Root>
				<Tooltip.Trigger>
					{#snippet child({ props })}
						<span {...props}>
							<HelpCircle class="h-3 w-3" />
						</span>
					{/snippet}
				</Tooltip.Trigger>
				<Tooltip.Content side="top" class="max-w-xs">{FIELD_DESCRIPTIONS[descKey]}</Tooltip.Content>
			</Tooltip.Root>
		{/if}
	</p>
{/snippet}

<Card.Root>
	<Card.Header>
		<Card.Title>General Information</Card.Title>
		<Card.Description>Namespace configuration and storage metrics</Card.Description>
	</Card.Header>
	<Card.Content>
		{#await nsData}
			<div class="grid grid-cols-2 gap-4 sm:grid-cols-4">
				{#each Array(8) as _, i (i)}
					<div class="space-y-1">
						<div class="h-3 w-20 animate-pulse rounded bg-muted"></div>
						<div class="h-4 w-28 animate-pulse rounded bg-muted"></div>
					</div>
				{/each}
			</div>
		{:then}
			{#if ns}
				<div class="grid grid-cols-2 gap-x-8 gap-y-3 sm:grid-cols-4">
					<div>
						{@render fieldLabel('Description', 'description')}
						<p class="mt-0.5 text-sm">{ns.description || '—'}</p>
					</div>
					<div>
						{@render fieldLabel('Owner', 'owner')}
						{#if editingOwner}
							<div class="mt-0.5 flex items-center gap-1">
								<Input
									class="h-7 w-36 text-sm"
									bind:value={ownerInput}
									placeholder="username"
									disabled={savingOwner}
									onkeydown={(e: KeyboardEvent) => {
										if (e.key === 'Enter') saveOwner();
										if (e.key === 'Escape') cancelEditOwner();
									}}
								/>
								<Button
									variant="ghost"
									size="icon"
									class="h-7 w-7"
									onclick={saveOwner}
									disabled={savingOwner}
								>
									<Check class="h-3.5 w-3.5" />
								</Button>
								<Button
									variant="ghost"
									size="icon"
									class="h-7 w-7"
									onclick={cancelEditOwner}
									disabled={savingOwner}
								>
									<X class="h-3.5 w-3.5" />
								</Button>
							</div>
						{:else}
							<p class="mt-0.5 flex items-center gap-1 text-sm">
								{ns.owner || '—'}
								<button
									class="inline-flex text-muted-foreground hover:text-foreground"
									onclick={startEditOwner}
									title="Change owner"
								>
									<Pencil class="h-3 w-3" />
								</button>
							</p>
						{/if}
					</div>
					<div>
						{@render fieldLabel('Hash Scheme', 'hashScheme')}
						<p class="mt-0.5 text-sm">{ns.hashScheme || '—'}</p>
					</div>
					<div>
						{@render fieldLabel('Created', 'created')}
						<p class="mt-0.5 text-sm">{ns.creationTime ? formatDate(ns.creationTime) : '—'}</p>
					</div>
					<div>
						{@render fieldLabel('Storage Used', 'storageUsed')}
						{#if ns.hardQuota}
							{@const quota = parseQuotaBytes(ns.hardQuota)}
							<p class="mt-0.5 text-sm">
								{formatBytes(nsStorageUsed)} / {ns.hardQuota}
							</p>
							{#if quota}
								{@const pct = (nsStorageUsed / quota) * 100}
								<StorageProgressBar percent={pct} class="mt-1 max-w-32" />
							{/if}
						{:else}
							<p class="mt-0.5 text-sm">{formatBytes(nsStorageUsed)}</p>
						{/if}
					</div>
					<div>
						{@render fieldLabel('Hard Quota', 'hardQuota')}
						{#if editingHardQuota}
							<div class="mt-0.5 flex items-center gap-1">
								<Input
									class="h-7 w-28 text-sm"
									bind:value={hardQuotaInput}
									placeholder="e.g. 50 GB"
									disabled={savingHardQuota}
									onkeydown={(e: KeyboardEvent) => {
										if (e.key === 'Enter') saveHardQuota();
										if (e.key === 'Escape') cancelEditHardQuota();
									}}
								/>
								<Button
									variant="ghost"
									size="icon"
									class="h-7 w-7"
									onclick={saveHardQuota}
									disabled={savingHardQuota}
								>
									<Check class="h-3.5 w-3.5" />
								</Button>
								<Button
									variant="ghost"
									size="icon"
									class="h-7 w-7"
									onclick={cancelEditHardQuota}
									disabled={savingHardQuota}
								>
									<X class="h-3.5 w-3.5" />
								</Button>
							</div>
						{:else}
							<p class="mt-0.5 flex items-center gap-1 text-sm">
								{ns.hardQuota || '—'}
								<button
									class="inline-flex text-muted-foreground hover:text-foreground"
									onclick={startEditHardQuota}
									title="Change hard quota"
								>
									<Pencil class="h-3 w-3" />
								</button>
							</p>
						{/if}
					</div>
					<div>
						{@render fieldLabel('Soft Quota', 'softQuota')}
						{#if editingSoftQuota}
							<div class="mt-0.5 flex items-center gap-1">
								<Input
									class="h-7 w-20 text-sm"
									type="number"
									min="10"
									max="95"
									bind:value={softQuotaInput}
									placeholder="10–95"
									disabled={savingSoftQuota}
									onkeydown={(e: KeyboardEvent) => {
										if (e.key === 'Enter') saveSoftQuota();
										if (e.key === 'Escape') cancelEditSoftQuota();
									}}
								/>
								<span class="text-sm text-muted-foreground">%</span>
								<Button
									variant="ghost"
									size="icon"
									class="h-7 w-7"
									onclick={saveSoftQuota}
									disabled={savingSoftQuota}
								>
									<Check class="h-3.5 w-3.5" />
								</Button>
								<Button
									variant="ghost"
									size="icon"
									class="h-7 w-7"
									onclick={cancelEditSoftQuota}
									disabled={savingSoftQuota}
								>
									<X class="h-3.5 w-3.5" />
								</Button>
							</div>
						{:else}
							<p class="mt-0.5 flex items-center gap-1 text-sm">
								{ns.softQuota != null ? `${ns.softQuota}%` : '—'}
								<button
									class="inline-flex text-muted-foreground hover:text-foreground"
									onclick={startEditSoftQuota}
									title="Change soft quota"
								>
									<Pencil class="h-3 w-3" />
								</button>
							</p>
						{/if}
					</div>
					<div>
						{@render fieldLabel('Objects', 'objects')}
						<p class="mt-0.5 text-sm font-medium">
							{stats ? (stats.objectCount ?? 0).toLocaleString() : '—'}
						</p>
					</div>
				</div>

				<!-- Inline stats row -->
				{#if stats}
					<div class="mt-4 flex flex-wrap gap-x-8 gap-y-2 border-t pt-3">
						<div class="text-sm">
							<span class="text-muted-foreground">Ingested:</span>
							<span class="font-medium">{formatBytes(Number(stats.ingestedVolume ?? 0))}</span>
						</div>
						<div class="text-sm">
							<span class="text-muted-foreground">Custom Metadata:</span>
							<span class="font-medium">{(stats.customMetadataCount ?? 0).toLocaleString()}</span>
							<span class="text-xs text-muted-foreground">
								({formatBytes(Number(stats.customMetadataSize ?? 0))})
							</span>
						</div>
						<div class="text-sm">
							<span class="text-muted-foreground">Shredded:</span>
							<span class="font-medium">{(stats.shredCount ?? 0).toLocaleString()}</span>
							<span class="text-xs text-muted-foreground">
								({formatBytes(Number(stats.shredSize ?? 0))})
							</span>
						</div>
					</div>
				{/if}
			{:else}
				<p class="text-center text-sm text-muted-foreground">
					Namespace not found or could not be loaded.
				</p>
			{/if}
		{/await}
	</Card.Content>
</Card.Root>
