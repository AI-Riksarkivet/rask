<script lang="ts">
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import * as Tabs from '$lib/components/ui/tabs/index.js';
	import { Database, Settings2, Pencil, Check, X } from 'lucide-svelte';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { toast } from 'svelte-sonner';
	import BackButton from '$lib/components/custom/back-button/back-button.svelte';
	import NoTenantPlaceholder from '$lib/components/custom/no-tenant-placeholder/no-tenant-placeholder.svelte';
	import NsGeneralInfo from './sections/ns-general-info.svelte';
	import NsProtocols from './sections/ns-protocols.svelte';
	import NsFeatures from './sections/ns-features.svelte';
	import NsTags from './sections/ns-tags.svelte';
	import NsUserAccess from './sections/ns-user-access.svelte';
	import NsChargeback from './sections/ns-chargeback.svelte';
	import NsCompliance from './sections/ns-compliance.svelte';
	import NsVersioning from './sections/ns-versioning.svelte';
	import NsRetentionClasses from './sections/ns-retention-classes.svelte';
	import NsIndexing from './sections/ns-indexing.svelte';
	import NsCors from './sections/ns-cors.svelte';
	import NsReplicationCollision from './sections/ns-replication-collision.svelte';
	import NsNetwork from './sections/ns-network.svelte';
	import {
		update_namespace,
		get_ns_protocols,
		update_ns_protocol,
		type NsProtocols as NsProtocolsType,
	} from '$lib/remote/namespaces.remote.js';
	import {
		get_users,
		get_user_permissions,
		set_user_permissions,
		get_groups,
		get_group_permissions,
		set_group_permissions,
		type DataAccessPermissions,
	} from '$lib/remote/users.remote.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let tenant = $derived(page.data.tenant as string | undefined);
	let namespaceName = $derived(page.params.namespace ?? '');

	let activeTab = $state('namespace');

	// Protocol state for rename flow
	let protocolsData = $derived(
		tenant ? get_ns_protocols({ tenant, name: namespaceName }) : undefined
	);
	let protocols = $derived((protocolsData?.current ?? {}) as NsProtocolsType);
	let hasBlockingProtocol = $derived(
		!!(
			protocols.httpsEnabled ||
			protocols.httpEnabled ||
			protocols.cifsEnabled ||
			protocols.nfsEnabled
		)
	);

	// Namespace rename
	let editingName = $state(false);
	let nameInput = $state('');
	let savingName = $state(false);

	const NS_NAME_RE = /^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$/;

	function startEditName() {
		nameInput = namespaceName;
		editingName = true;
	}

	function cancelEditName() {
		editingName = false;
	}

	async function saveName() {
		if (!tenant) return;
		const trimmed = nameInput.trim();
		if (trimmed === namespaceName) {
			editingName = false;
			return;
		}
		if (!trimmed || trimmed.length > 63 || !NS_NAME_RE.test(trimmed)) {
			toast.error(
				'Name must be 1–63 characters, alphanumeric and hyphens only, cannot start/end with hyphen.'
			);
			return;
		}
		if (trimmed.startsWith('xn--')) {
			toast.error('Namespace names cannot start with "xn--".');
			return;
		}
		savingName = true;
		try {
			// Capture which protocols need temporarily disabling
			const hadHttp = protocols.httpEnabled;
			const hadHttps = protocols.httpsEnabled;
			const hadCifs = protocols.cifsEnabled;
			const hadNfs = protocols.nfsEnabled;

			// Step 1: Disable blocking protocols
			if (hadHttp || hadHttps) {
				await update_ns_protocol({
					tenant,
					name: namespaceName,
					protocol: 'http',
					body: { httpEnabled: false, httpsEnabled: false },
				});
			}
			if (hadCifs) {
				await update_ns_protocol({
					tenant,
					name: namespaceName,
					protocol: 'cifs',
					body: { cifsEnabled: false },
				});
			}
			if (hadNfs) {
				await update_ns_protocol({
					tenant,
					name: namespaceName,
					protocol: 'nfs',
					body: { nfsEnabled: false },
				});
			}

			// Step 2: Rename
			await update_namespace({
				tenant,
				name: namespaceName,
				body: { name: trimmed },
			});

			// Step 3: Re-POST data access permissions to refresh HCP S3 gateway
			// HCP GET returns read-only fields (readAllowed, etc.) that POST rejects,
			// so we must rebuild a clean body with only namespaceName + permissions.
			try {
				function cleanPerms(raw: DataAccessPermissions): Record<string, unknown> {
					return {
						namespacePermission: (raw.namespacePermission ?? []).map((e) => ({
							namespaceName: e.namespaceName,
							permissions: { permission: e.permissions?.permission ?? [] },
						})),
					};
				}

				const [users, groups] = await Promise.all([
					get_users({ tenant }) as Promise<Array<{ username: string }>>,
					get_groups({ tenant }) as Promise<Array<{ groupname: string }>>,
				]);
				await Promise.all([
					...users.map(async (u) => {
						const perms = (await get_user_permissions({
							tenant,
							username: u.username,
						})) as DataAccessPermissions;
						const hasAccess = (perms.namespacePermission ?? []).some(
							(e) => e.namespaceName === trimmed
						);
						if (hasAccess) {
							await set_user_permissions({
								tenant,
								username: u.username,
								body: cleanPerms(perms),
							});
						}
					}),
					...groups.map(async (g) => {
						const perms = (await get_group_permissions({
							tenant,
							groupname: g.groupname,
						})) as DataAccessPermissions;
						const hasAccess = (perms.namespacePermission ?? []).some(
							(e) => e.namespaceName === trimmed
						);
						if (hasAccess) {
							await set_group_permissions({
								tenant,
								groupname: g.groupname,
								body: cleanPerms(perms),
							});
						}
					}),
				]);
			} catch {
				// Non-fatal: permissions may need manual re-save
				console.warn('Could not refresh data access permissions after rename');
			}

			// Step 4: Re-enable protocols on the renamed namespace
			if (hadHttp || hadHttps) {
				await update_ns_protocol({
					tenant,
					name: trimmed,
					protocol: 'http',
					body: { httpEnabled: hadHttp, httpsEnabled: hadHttps },
				});
			}
			if (hadCifs) {
				await update_ns_protocol({
					tenant,
					name: trimmed,
					protocol: 'cifs',
					body: { cifsEnabled: true },
				});
			}
			if (hadNfs) {
				await update_ns_protocol({
					tenant,
					name: trimmed,
					protocol: 'nfs',
					body: { nfsEnabled: true },
				});
			}

			toast.success(`Namespace renamed to "${trimmed}"`);
			editingName = false;
			goto(`/namespaces/${encodeURIComponent(trimmed)}`, { replaceState: true });
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to rename namespace'));
		} finally {
			savingName = false;
		}
	}
</script>

<svelte:head>
	<title>{namespaceName} - Namespace Settings - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<!-- Header -->
	<div class="flex items-center gap-4">
		<BackButton href="/namespaces" label="Back to namespaces" />
		<div>
			{#if editingName}
				<div class="flex items-center gap-2">
					<Input
						class="h-9 w-64 text-lg font-bold"
						bind:value={nameInput}
						placeholder="namespace-name"
						disabled={savingName}
						onkeydown={(e: KeyboardEvent) => {
							if (e.key === 'Enter') saveName();
							if (e.key === 'Escape') cancelEditName();
						}}
					/>
					<Button
						variant="ghost"
						size="icon"
						class="h-8 w-8"
						onclick={saveName}
						disabled={savingName}
					>
						<Check class="h-4 w-4" />
					</Button>
					<Button
						variant="ghost"
						size="icon"
						class="h-8 w-8"
						onclick={cancelEditName}
						disabled={savingName}
					>
						<X class="h-4 w-4" />
					</Button>
				</div>
			{:else}
				<h2 class="flex items-center gap-2 text-2xl font-bold">
					{namespaceName}
					{#if tenant}
						<button
							class="inline-flex text-muted-foreground hover:text-foreground"
							onclick={startEditName}
							title="Rename namespace"
						>
							<Pencil class="h-4 w-4" />
						</button>
					{/if}
				</h2>
			{/if}
			<p class="mt-1 text-sm text-muted-foreground">Namespace settings and access control</p>
		</div>
	</div>

	{#if !tenant}
		<NoTenantPlaceholder message="Log in with a tenant to view namespace details." />
	{:else}
		<Tabs.Root bind:value={activeTab}>
			<Tabs.List>
				<Tabs.Trigger value="namespace">
					<Database class="mr-1.5 h-4 w-4" />
					Namespace
				</Tabs.Trigger>
				<Tabs.Trigger value="settings">
					<Settings2 class="mr-1.5 h-4 w-4" />
					Settings
				</Tabs.Trigger>
			</Tabs.List>

			<Tabs.Content value="namespace" class="space-y-6">
				<NsGeneralInfo {tenant} {namespaceName} />
				<NsChargeback {tenant} {namespaceName} />
				<NsUserAccess {tenant} {namespaceName} />
			</Tabs.Content>

			<Tabs.Content value="settings" class="space-y-6">
				<div class="grid items-stretch gap-6 lg:grid-cols-3">
					<NsProtocols {tenant} {namespaceName} />
					<NsFeatures {tenant} {namespaceName} />
					<NsTags {tenant} {namespaceName} />
				</div>

				<div class="grid items-stretch gap-6 lg:grid-cols-3">
					<NsCompliance {tenant} {namespaceName} />
					<NsVersioning {tenant} {namespaceName} />
					<NsRetentionClasses {tenant} {namespaceName} />
				</div>

				<div class="grid items-stretch gap-6 lg:grid-cols-3">
					<NsIndexing {tenant} {namespaceName} />
					<NsCors {tenant} {namespaceName} />
					<NsReplicationCollision {tenant} {namespaceName} />
				</div>

				<NsNetwork {tenant} {namespaceName} />
			</Tabs.Content>
		</Tabs.Root>
	{/if}
</div>
