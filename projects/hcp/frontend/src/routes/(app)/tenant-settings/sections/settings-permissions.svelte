<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import { Switch } from '$lib/components/ui/switch/index.js';
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
	import { HelpCircle } from 'lucide-svelte';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import SaveButton from '$lib/components/custom/save-button/save-button.svelte';
	import { useSave } from '$lib/utils/use-save.svelte.js';
	import {
		get_tenant_settings,
		update_permissions,
		type TenantSettings,
	} from '$lib/remote/tenant-info.remote.js';

	let {
		tenant,
	}: {
		tenant: string;
	} = $props();

	const PERMISSION_KEYS = [
		'namespaceDeleteAllowed',
		'namespaceManageAllowed',
		'namespaceUndeleteAllowed',
		'erasureCodingAllowed',
		'searchAllowed',
		'replicationAllowed',
		'complianceAllowed',
		'taggingAllowed',
	] as const;

	const PERMISSION_DESCRIPTIONS: Record<string, string> = {
		namespaceDeleteAllowed: 'Allow tenant users to delete namespaces',
		namespaceManageAllowed: 'Allow tenant users to modify namespace settings',
		namespaceUndeleteAllowed: 'Allow recovery of deleted namespaces',
		erasureCodingAllowed: 'Enable erasure coding for storage efficiency',
		searchAllowed: 'Allow use of HCP metadata search engine',
		replicationAllowed: 'Allow namespace data replication',
		complianceAllowed: 'Allow configuration of compliance and retention policies',
		taggingAllowed: 'Allow tagging of namespaces and objects',
	};

	let settingsData = $derived(get_tenant_settings({ tenant }));
	let settings = $derived((settingsData?.current ?? null) as TenantSettings | null);

	const saver = useSave({
		successMsg: 'Permissions updated successfully',
		errorMsg: 'Failed to update permissions',
	});

	let localPermissions = $state<Record<string, boolean>>({});

	$effect(() => {
		const s = settings;
		void saver.syncVersion;
		const perms: Record<string, boolean> = {};
		for (const key of PERMISSION_KEYS) {
			perms[key] = (s?.permissions?.[key] as boolean) ?? false;
		}
		localPermissions = perms;
	});

	let dirty = $derived(
		PERMISSION_KEYS.some(
			(key) =>
				(localPermissions[key] ?? false) !== ((settings?.permissions?.[key] as boolean) ?? false)
		)
	);

	function togglePermission(key: string) {
		localPermissions = { ...localPermissions, [key]: !localPermissions[key] };
	}

	function formatKey(key: string): string {
		return key.replace(/([A-Z])/g, ' $1').replace(/^./, (s) => s.toUpperCase());
	}
</script>

{#await settingsData}
	<CardSkeleton />
{:then}
	<Card.Root>
		<Card.Header>
			<Card.Title>Permissions</Card.Title>
			<Card.Description>Tenant-level permission settings</Card.Description>
		</Card.Header>
		<Card.Content>
			{#if settings}
				<div class="space-y-4">
					<div class="space-y-3">
						{#each PERMISSION_KEYS as key (key)}
							<div class="flex items-center justify-between">
								<span class="flex items-center gap-1.5 text-sm text-muted-foreground">
									{formatKey(key)}
									<Tooltip.Root>
										<Tooltip.Trigger>
											{#snippet child({ props })}
												<span {...props} class="inline-flex">
													<HelpCircle class="h-3.5 w-3.5" />
												</span>
											{/snippet}
										</Tooltip.Trigger>
										<Tooltip.Content side="left">{PERMISSION_DESCRIPTIONS[key]}</Tooltip.Content>
									</Tooltip.Root>
								</span>
								<Switch
									checked={localPermissions[key] ?? false}
									onCheckedChange={() => togglePermission(key)}
								/>
							</div>
						{/each}
					</div>
					<SaveButton
						{dirty}
						saving={saver.saving}
						onclick={() =>
							saver.run(async () => {
								if (!settingsData) return;
								await update_permissions({
									tenant,
									body: { ...localPermissions },
								}).updates(settingsData);
							})}
					/>
				</div>
			{/if}
		</Card.Content>
	</Card.Root>
{/await}
