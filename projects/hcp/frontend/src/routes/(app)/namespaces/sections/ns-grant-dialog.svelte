<script lang="ts">
	import { SvelteSet } from 'svelte/reactivity';
	import { Loader2, HelpCircle } from 'lucide-svelte';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Checkbox } from '$lib/components/ui/checkbox/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import * as Dialog from '$lib/components/ui/dialog/index.js';
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
	import { toast } from 'svelte-sonner';
	import { PERMISSION_DESCRIPTIONS } from '$lib/constants.js';
	import {
		get_users,
		get_user_permissions,
		set_user_permissions,
		type DataAccessPermissions,
	} from '$lib/remote/users.remote.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	const PERMISSION_KEYS = ['READ', 'WRITE', 'DELETE', 'PURGE', 'SEARCH', 'BROWSE'] as const;

	let {
		tenant,
		namespaceNames = [],
		open = $bindable(false),
		ongranted,
	}: {
		tenant: string;
		namespaceNames: string[];
		open: boolean;
		ongranted?: () => void;
	} = $props();

	let usersData = $derived(open ? get_users({ tenant }) : null);
	let users = $derived(
		((usersData?.current ?? []) as { username: string }[]).map((u) => u.username)
	);

	let selectedUser = $state('');
	let grantPerms = new SvelteSet<string>();
	let granting = $state(false);
	let grantError = $state('');

	function resetFormState() {
		selectedUser = '';
		grantPerms.clear();
		grantError = '';
	}

	// Reset state when dialog opens
	$effect(() => {
		if (!open) return;
		// This intentionally resets form state - it's the expected behavior when opening the dialog
		resetFormState();
	});

	function togglePerm(perm: string) {
		if (grantPerms.has(perm)) {
			grantPerms.delete(perm);
		} else {
			grantPerms.add(perm);
		}
	}

	async function handleGrant() {
		if (!selectedUser || grantPerms.size === 0 || namespaceNames.length === 0) return;
		granting = true;
		grantError = '';

		try {
			// Fetch existing permissions for the user
			const existing = await get_user_permissions({ tenant, username: selectedUser });

			// Preserve permissions for namespaces NOT in our selection
			const otherNsPerms = (existing?.namespacePermission ?? []).filter(
				(e) => !namespaceNames.includes(e.namespaceName)
			);

			// Build new entries for each selected namespace
			const newNsPerms = namespaceNames.map((ns) => ({
				namespaceName: ns,
				permissions: { permission: [...grantPerms] },
			}));

			const body: DataAccessPermissions = {
				namespacePermission: [...otherNsPerms, ...newNsPerms],
			};

			await set_user_permissions({
				tenant,
				username: selectedUser,
				body: body as Record<string, unknown>,
			});

			toast.success(`Granted access to ${selectedUser} on ${namespaceNames.length} namespace(s)`);
			open = false;
			ongranted?.();
		} catch (err) {
			grantError = getErrorMessage(err, 'Failed to grant access');
		} finally {
			granting = false;
		}
	}
</script>

<Dialog.Root bind:open>
	<Dialog.Content class="sm:max-w-lg">
		<Dialog.Header>
			<Dialog.Title>Grant Namespace Access</Dialog.Title>
			<Dialog.Description>
				Grant a user data access permissions on the selected namespaces.
			</Dialog.Description>
		</Dialog.Header>

		<div class="space-y-4">
			<!-- Selected namespaces -->
			<div class="space-y-2">
				<Label>Namespaces</Label>
				<div class="flex flex-wrap gap-1.5">
					{#each namespaceNames as ns (ns)}
						<Badge variant="secondary">{ns}</Badge>
					{/each}
				</div>
			</div>

			<!-- User selector -->
			<div class="space-y-2">
				<Label for="grant-user">User</Label>
				<select
					id="grant-user"
					class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-9 w-full items-center rounded-md border px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
					bind:value={selectedUser}
				>
					<option value="">Select a user...</option>
					{#each users as u (u)}
						<option value={u}>{u}</option>
					{/each}
				</select>
			</div>

			<!-- Permissions -->
			<div class="space-y-2">
				<Label>Permissions</Label>
				<div class="space-y-2 rounded-md border p-3">
					{#each PERMISSION_KEYS as perm (perm)}
						<div class="flex items-center gap-2">
							<Checkbox checked={grantPerms.has(perm)} onCheckedChange={() => togglePerm(perm)} />
							<span class="text-sm font-medium">{perm}</span>
							{#if PERMISSION_DESCRIPTIONS[perm]}
								<Tooltip.Provider>
									<Tooltip.Root>
										<Tooltip.Trigger>
											<HelpCircle class="h-3.5 w-3.5 text-muted-foreground" />
										</Tooltip.Trigger>
										<Tooltip.Content>
											<p>{PERMISSION_DESCRIPTIONS[perm]}</p>
										</Tooltip.Content>
									</Tooltip.Root>
								</Tooltip.Provider>
							{/if}
						</div>
					{/each}
				</div>
			</div>

			{#if grantError}
				<p class="text-sm text-destructive">{grantError}</p>
			{/if}
		</div>

		<Dialog.Footer>
			<Button variant="ghost" onclick={() => (open = false)} disabled={granting}>Cancel</Button>
			<Button onclick={handleGrant} disabled={granting || !selectedUser || grantPerms.size === 0}>
				{#if granting}
					<Loader2 class="h-4 w-4 animate-spin" />
					Granting...
				{:else}
					Grant Access
				{/if}
			</Button>
		</Dialog.Footer>
	</Dialog.Content>
</Dialog.Root>
