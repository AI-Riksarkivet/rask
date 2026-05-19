<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import SaveButton from '$lib/components/custom/save-button/save-button.svelte';
	import { useSave } from '$lib/utils/use-save.svelte.js';
	import type { DataAccessPermissions } from '$lib/remote/users.remote.js';
	import { get_namespaces, type Namespace } from '$lib/remote/namespaces.remote.js';
	import { PERMISSION_DESCRIPTIONS } from '$lib/constants.js';
	import { Plus, Trash2, Shield } from 'lucide-svelte';
	import type { RemoteQuery } from '@sveltejs/kit';

	let {
		tenant,
		label = 'user',
		permsData,
		onsave,
	}: {
		tenant: string;
		label?: string;
		permsData: RemoteQuery<DataAccessPermissions> | undefined;
		onsave: (body: DataAccessPermissions) => Promise<void>;
	} = $props();

	const ALL_PERMISSIONS = Object.keys(PERMISSION_DESCRIPTIONS);

	let nsData = $derived(get_namespaces({ tenant }));
	let allNamespaces = $derived((nsData?.current ?? []) as Namespace[]);

	const saver = useSave({
		successMsg: 'Namespace permissions updated',
		errorMsg: 'Failed to update permissions',
	});

	type NsPermEntry = { namespaceName: string; permissions: string[] };

	let localPerms = $state<NsPermEntry[]>([]);

	$effect(() => {
		const current = (permsData?.current as DataAccessPermissions) ?? {};
		void saver.syncVersion;
		localPerms = (current.namespacePermission ?? [])
			.filter((e) => e.permissions?.permission && e.permissions.permission.length > 0)
			.map((e) => ({
				namespaceName: e.namespaceName,
				permissions: [...(e.permissions?.permission ?? [])],
			}));
	});

	let assignedNamespaces = $derived(new Set(localPerms.map((e) => e.namespaceName)));
	let availableNamespaces = $derived(
		allNamespaces.filter((ns) => !assignedNamespaces.has(ns.name))
	);

	let dirty = $derived.by(() => {
		const current = (permsData?.current as DataAccessPermissions) ?? {};
		const serverEntries = (current.namespacePermission ?? []).filter(
			(e) => e.permissions?.permission && e.permissions.permission.length > 0
		);
		if (localPerms.length !== serverEntries.length) return true;
		for (const local of localPerms) {
			const server = serverEntries.find((s) => s.namespaceName === local.namespaceName);
			if (!server) return true;
			const serverPerms = [...(server.permissions?.permission ?? [])].sort();
			const localSorted = [...local.permissions].sort();
			if (serverPerms.length !== localSorted.length) return true;
			for (let i = 0; i < serverPerms.length; i++) {
				if (serverPerms[i] !== localSorted[i]) return true;
			}
		}
		return false;
	});

	let addingNs = $state('');

	function addNamespace() {
		if (!addingNs || assignedNamespaces.has(addingNs)) return;
		localPerms = [...localPerms, { namespaceName: addingNs, permissions: ['BROWSE', 'READ'] }];
		addingNs = '';
	}

	function removeNamespace(nsName: string) {
		localPerms = localPerms.filter((e) => e.namespaceName !== nsName);
	}

	function togglePermission(nsName: string, perm: string) {
		localPerms = localPerms.map((e) => {
			if (e.namespaceName !== nsName) return e;
			const has = e.permissions.includes(perm);
			return {
				...e,
				permissions: has ? e.permissions.filter((p) => p !== perm) : [...e.permissions, perm],
			};
		});
	}

	function toggleAll(nsName: string) {
		localPerms = localPerms.map((e) => {
			if (e.namespaceName !== nsName) return e;
			const allSelected = ALL_PERMISSIONS.every((p) => e.permissions.includes(p));
			return { ...e, permissions: allSelected ? [] : [...ALL_PERMISSIONS] };
		});
	}

	function buildBody(): DataAccessPermissions {
		return {
			namespacePermission: localPerms
				.filter((e) => e.permissions.length > 0)
				.map((e) => ({
					namespaceName: e.namespaceName,
					permissions: { permission: e.permissions },
				})),
		};
	}
</script>

<Card.Root>
	<Card.Header>
		<div class="flex items-center gap-2">
			<Shield class="h-5 w-5 text-muted-foreground" />
			<div>
				<Card.Title>Namespace Access</Card.Title>
				<Card.Description
					>Manage which namespaces this {label} can access and their permissions</Card.Description
				>
			</div>
		</div>
	</Card.Header>
	<Card.Content class="space-y-4">
		{#await permsData}
			<div class="space-y-2">
				{#each Array(3) as _, i (i)}
					<div class="h-10 w-full animate-pulse rounded bg-muted"></div>
				{/each}
			</div>
		{:then}
			{#if localPerms.length === 0}
				<p class="py-4 text-center text-sm text-muted-foreground">
					This {label} has no namespace access. Add a namespace below.
				</p>
			{:else}
				<div class="space-y-3">
					{#each localPerms as entry (entry.namespaceName)}
						<div class="space-y-2 rounded-md border p-3">
							<div class="flex items-center justify-between">
								<a
									href="/namespaces/{entry.namespaceName}"
									class="text-sm font-medium text-primary underline-offset-4 hover:underline"
								>
									{entry.namespaceName}
								</a>
								<div class="flex items-center gap-2">
									<button
										type="button"
										class="text-xs text-muted-foreground hover:text-foreground"
										onclick={() => toggleAll(entry.namespaceName)}
									>
										{#if ALL_PERMISSIONS.every((p) => entry.permissions.includes(p))}
											Clear all
										{:else}
											Select all
										{/if}
									</button>
									<Button
										variant="ghost"
										size="icon"
										class="h-7 w-7 text-muted-foreground hover:text-destructive"
										onclick={() => removeNamespace(entry.namespaceName)}
									>
										<Trash2 class="h-3.5 w-3.5" />
									</Button>
								</div>
							</div>
							<div class="flex flex-wrap gap-1.5">
								{#each ALL_PERMISSIONS as perm (perm)}
									<Tooltip.Root>
										<Tooltip.Trigger>
											{#snippet child({ props })}
												<button
													{...props}
													type="button"
													onclick={() => togglePermission(entry.namespaceName, perm)}
												>
													<Badge
														variant={entry.permissions.includes(perm) ? 'default' : 'outline'}
														class="cursor-pointer select-none transition-colors {entry.permissions.includes(
															perm
														)
															? ''
															: 'opacity-50'}"
													>
														{perm}
													</Badge>
												</button>
											{/snippet}
										</Tooltip.Trigger>
										<Tooltip.Content>{PERMISSION_DESCRIPTIONS[perm]}</Tooltip.Content>
									</Tooltip.Root>
								{/each}
							</div>
						</div>
					{/each}
				</div>
			{/if}

			<div class="flex items-end gap-2">
				<div class="flex-1 space-y-1">
					<Label for="add-ns-{label}" class="text-xs">Add Namespace</Label>
					<select
						id="add-ns-{label}"
						class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-9 w-full items-center rounded-md border px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
						bind:value={addingNs}
					>
						<option value="" disabled>Select namespace...</option>
						{#each availableNamespaces as ns (ns.name)}
							<option value={ns.name}>{ns.name}</option>
						{/each}
					</select>
				</div>
				<Button variant="outline" size="sm" onclick={addNamespace} disabled={!addingNs}>
					<Plus class="h-4 w-4" />
					Add
				</Button>
			</div>
		{/await}
	</Card.Content>
	<Card.Footer class="flex justify-end">
		<SaveButton
			{dirty}
			saving={saver.saving}
			onclick={() => saver.run(async () => onsave(buildBody()))}
		/>
	</Card.Footer>
</Card.Root>
