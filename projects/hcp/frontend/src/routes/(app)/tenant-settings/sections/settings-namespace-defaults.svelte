<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Switch } from '$lib/components/ui/switch/index.js';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import SaveButton from '$lib/components/custom/save-button/save-button.svelte';
	import { useSave } from '$lib/utils/use-save.svelte.js';
	import {
		get_tenant_settings,
		update_namespace_defaults,
		type TenantSettings,
	} from '$lib/remote/tenant-info.remote.js';

	let {
		tenant,
	}: {
		tenant: string;
	} = $props();

	let settingsData = $derived(get_tenant_settings({ tenant }));
	let settings = $derived((settingsData?.current ?? null) as TenantSettings | null);

	const saver = useSave({
		successMsg: 'Namespace defaults updated successfully',
		errorMsg: 'Failed to update namespace defaults',
	});

	let localHardQuota = $state('');
	let localSoftQuota = $state('');
	let localOptimizedFor = $state('');
	let localHashScheme = $state('');
	let localSearchEnabled = $state(false);
	let localVersioningEnabled = $state(false);
	let localUseDeleteMarkers = $state(false);

	$effect(() => {
		const s = settings;
		void saver.syncVersion;
		localHardQuota = (s?.namespaceDefaults?.hardQuota as string) ?? '';
		localSoftQuota = (s?.namespaceDefaults?.softQuota as string) ?? '';
		localOptimizedFor = (s?.namespaceDefaults?.optimizedFor as string) ?? '';
		localHashScheme = (s?.namespaceDefaults?.hashScheme as string) ?? '';
		localSearchEnabled = (s?.namespaceDefaults?.searchEnabled as boolean) ?? false;
		localVersioningEnabled = (s?.namespaceDefaults?.versioningEnabled as boolean) ?? false;
		localUseDeleteMarkers = (s?.namespaceDefaults?.useDeleteMarkers as boolean) ?? false;
	});

	let dirty = $derived(
		localHardQuota !== ((settings?.namespaceDefaults?.hardQuota as string) ?? '') ||
			localSoftQuota !== ((settings?.namespaceDefaults?.softQuota as string) ?? '') ||
			localOptimizedFor !== ((settings?.namespaceDefaults?.optimizedFor as string) ?? '') ||
			localHashScheme !== ((settings?.namespaceDefaults?.hashScheme as string) ?? '') ||
			localSearchEnabled !== ((settings?.namespaceDefaults?.searchEnabled as boolean) ?? false) ||
			localVersioningEnabled !==
				((settings?.namespaceDefaults?.versioningEnabled as boolean) ?? false) ||
			localUseDeleteMarkers !==
				((settings?.namespaceDefaults?.useDeleteMarkers as boolean) ?? false)
	);
</script>

{#await settingsData}
	<CardSkeleton />
{:then}
	<Card.Root>
		<Card.Header>
			<Card.Title>Namespace Defaults</Card.Title>
			<Card.Description>Default settings for new namespaces</Card.Description>
		</Card.Header>
		<Card.Content>
			{#if settings}
				<div class="space-y-4">
					<div class="grid gap-4 sm:grid-cols-2">
						<div class="space-y-2">
							<Label for="ns-hard-quota">Hard Quota</Label>
							<Input id="ns-hard-quota" bind:value={localHardQuota} placeholder="e.g. 10 GB" />
						</div>
						<div class="space-y-2">
							<Label for="ns-soft-quota">Soft Quota</Label>
							<Input id="ns-soft-quota" bind:value={localSoftQuota} placeholder="e.g. 85" />
						</div>
						<div class="space-y-2">
							<Label for="ns-optimized-for">Optimized For</Label>
							<select
								id="ns-optimized-for"
								class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-9 w-full items-center rounded-md border px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
								bind:value={localOptimizedFor}
							>
								<option value="" disabled>Select...</option>
								<option value="CLOUD">CLOUD</option>
								<option value="DEFAULT">DEFAULT</option>
							</select>
						</div>
						<div class="space-y-2">
							<Label for="ns-hash-scheme">Hash Scheme</Label>
							<select
								id="ns-hash-scheme"
								class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-9 w-full items-center rounded-md border px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
								bind:value={localHashScheme}
							>
								<option value="" disabled>Select...</option>
								<option value="MD5">MD5</option>
								<option value="SHA-256">SHA-256</option>
								<option value="SHA-384">SHA-384</option>
								<option value="SHA-512">SHA-512</option>
							</select>
						</div>
					</div>
					<div class="flex flex-wrap gap-x-8 gap-y-4">
						<div class="flex items-center gap-2">
							<Switch id="ns-search" bind:checked={localSearchEnabled} />
							<Label for="ns-search" class="text-sm">Search Enabled</Label>
						</div>
						<div class="flex items-center gap-2">
							<Switch id="ns-versioning" bind:checked={localVersioningEnabled} />
							<Label for="ns-versioning" class="text-sm">Versioning Enabled</Label>
						</div>
					</div>
					{#if localVersioningEnabled}
						<div class="rounded-md border bg-muted/30 p-3 space-y-3">
							<p class="text-xs font-medium text-muted-foreground">Default Versioning Settings</p>
							<div class="space-y-1">
								<div class="flex items-center gap-2">
									<Switch id="ns-delete-markers" bind:checked={localUseDeleteMarkers} />
									<Label for="ns-delete-markers" class="text-sm">Use Delete Markers</Label>
								</div>
								<p class="ml-9 text-xs text-muted-foreground">
									Create delete markers instead of permanently removing objects. Irreversible once
									enabled.
								</p>
							</div>
						</div>
					{/if}
					<SaveButton
						{dirty}
						saving={saver.saving}
						onclick={() =>
							saver.run(async () => {
								if (!settingsData) return;
								await update_namespace_defaults({
									tenant,
									body: {
										hardQuota: localHardQuota,
										softQuota: localSoftQuota,
										optimizedFor: localOptimizedFor,
										hashScheme: localHashScheme,
										searchEnabled: localSearchEnabled,
										versioningEnabled: localVersioningEnabled,
										useDeleteMarkers: localUseDeleteMarkers,
									},
								}).updates(settingsData);
							})}
					/>
				</div>
			{/if}
		</Card.Content>
	</Card.Root>
{/await}
