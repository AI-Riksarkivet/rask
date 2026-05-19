<script lang="ts">
	import { SvelteSet } from 'svelte/reactivity';
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
	import * as Dialog from '$lib/components/ui/dialog/index.js';
	import * as Card from '$lib/components/ui/card/index.js';
	import * as Table from '$lib/components/ui/table/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Checkbox } from '$lib/components/ui/checkbox/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Save, Loader2, Plus, HelpCircle } from 'lucide-svelte';
	import { toast } from 'svelte-sonner';
	import { PERMISSION_DESCRIPTIONS } from '$lib/constants.js';
	import TableSkeleton from '$lib/components/ui/skeleton/table-skeleton.svelte';
	import {
		get_users,
		get_user_permissions,
		set_user_permissions,
		type DataAccessPermissions,
	} from '$lib/remote/users.remote.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let {
		tenant,
		namespaceName,
	}: {
		tenant: string;
		namespaceName: string;
	} = $props();

	const PERMISSION_KEYS = ['READ', 'WRITE', 'DELETE', 'PURGE', 'SEARCH', 'BROWSE'] as const;

	interface UserPermState {
		fullData: DataAccessPermissions;
		permissions: SvelteSet<string>;
		loading: boolean;
		saving: boolean;
		dirty: boolean;
		originalPermissions: SvelteSet<string>;
	}

	let allUsers = $state.raw<Array<{ username: string }>>([]);
	let userPermMap = $state<Record<string, UserPermState>>({});
	let accessLoading = $state(true);
	let accessVersion = $state(0);

	let usersWithAccess = $derived(
		Object.entries(userPermMap)
			.filter(
				([, entry]) =>
					!entry.loading && (entry.originalPermissions.size > 0 || entry.permissions.size > 0)
			)
			.map(([username]) => username)
	);

	let usersWithoutAccess = $derived(
		allUsers.filter(
			(u) =>
				!userPermMap[u.username] ||
				userPermMap[u.username].loading ||
				(userPermMap[u.username].permissions.size === 0 &&
					userPermMap[u.username].originalPermissions.size === 0)
		)
	);

	$effect(() => {
		const currentTenant = tenant;
		const currentNs = namespaceName;
		void accessVersion;
		if (!currentTenant || !currentNs) {
			accessLoading = false;
			return;
		}

		accessLoading = true;
		let cancelled = false;

		(async () => {
			try {
				const fetchedUsers = (await get_users({ tenant: currentTenant })) as Array<{
					username: string;
				}>;
				if (cancelled) return;
				allUsers = fetchedUsers;

				const permResults = await Promise.all(
					fetchedUsers.map(async (user) => {
						try {
							const data = (await get_user_permissions({
								tenant: currentTenant,
								username: user.username,
							})) as DataAccessPermissions;
							const nsEntry = (data.namespacePermission ?? []).find(
								(entry) => entry.namespaceName === currentNs
							);
							const permList = nsEntry?.permissions?.permission ?? [];
							return { username: user.username, fullData: data, permList };
						} catch {
							return {
								username: user.username,
								fullData: {} as DataAccessPermissions,
								permList: [] as string[],
							};
						}
					})
				);

				if (cancelled) return;
				const map: Record<string, UserPermState> = {};
				for (const r of permResults) {
					map[r.username] = {
						fullData: r.fullData,
						permissions: new SvelteSet(r.permList),
						loading: false,
						saving: false,
						dirty: false,
						originalPermissions: new SvelteSet(r.permList),
					};
				}
				userPermMap = map;
			} catch {
				if (cancelled) return;
				userPermMap = {};
			} finally {
				if (!cancelled) accessLoading = false;
			}
		})();

		return () => {
			cancelled = true;
		};
	});

	function toggleUserPerm(username: string, perm: string) {
		const entry = userPermMap[username];
		if (!entry) return;

		const next = new SvelteSet(entry.permissions);
		if (next.has(perm)) {
			next.delete(perm);
		} else {
			next.add(perm);
		}

		const originalPerms = entry.originalPermissions;
		const isDirty =
			next.size !== originalPerms.size || [...next].some((p) => !originalPerms.has(p));

		userPermMap[username] = {
			...entry,
			permissions: next,
			dirty: isDirty,
		};
	}

	async function saveUserPerms(username: string) {
		const entry = userPermMap[username];
		if (!entry) return;

		userPermMap[username] = { ...entry, saving: true };

		try {
			const otherNsPerms = (entry.fullData.namespacePermission ?? []).filter(
				(e) => e.namespaceName !== namespaceName
			);

			const updatedBody: DataAccessPermissions = {
				namespacePermission: [
					...otherNsPerms,
					{
						namespaceName,
						permissions: { permission: [...entry.permissions] },
					},
				],
			};

			await set_user_permissions({
				tenant,
				username,
				body: updatedBody as Record<string, unknown>,
			});

			const perms = new SvelteSet(entry.permissions);
			userPermMap[username] = {
				...entry,
				fullData: updatedBody,
				saving: false,
				dirty: false,
				originalPermissions: perms,
			};

			toast.success(`Permissions updated for ${username}`);
		} catch {
			userPermMap[username] = { ...entry, saving: false };
			toast.error(`Failed to update permissions for ${username}`);
		}
	}

	// --- Grant Access dialog ---
	let grantOpen = $state(false);
	let grantUser = $state('');
	let grantPerms = new SvelteSet<string>();
	let granting = $state(false);
	let grantError = $state('');

	function openGrantDialog() {
		grantUser = '';
		grantPerms.clear();
		grantError = '';
		grantOpen = true;
	}

	async function handleGrant(e: SubmitEvent) {
		e.preventDefault();
		if (!grantUser || grantPerms.size === 0) return;
		granting = true;
		grantError = '';

		try {
			const existing = userPermMap[grantUser];
			const otherNsPerms = (existing?.fullData.namespacePermission ?? []).filter(
				(e2) => e2.namespaceName !== namespaceName
			);

			const body: DataAccessPermissions = {
				namespacePermission: [
					...otherNsPerms,
					{
						namespaceName,
						permissions: { permission: [...grantPerms] },
					},
				],
			};

			await set_user_permissions({
				tenant,
				username: grantUser,
				body: body as Record<string, unknown>,
			});

			toast.success(`Access granted to ${grantUser}`);
			grantOpen = false;
			accessVersion++;
		} catch (err) {
			grantError = getErrorMessage(err, 'Failed to grant access');
		} finally {
			granting = false;
		}
	}

	function toggleGrantPerm(perm: string) {
		if (grantPerms.has(perm)) {
			grantPerms.delete(perm);
		} else {
			grantPerms.add(perm);
		}
	}
</script>

<Card.Root>
	<Card.Header>
		<Card.Title>User Access</Card.Title>
		<Card.Description>Manage per-user data access permissions for this namespace.</Card.Description>
		<Card.Action>
			<div class="flex items-center gap-3">
				<a href="/users" class="text-sm text-primary underline-offset-4 hover:underline">
					Manage users
				</a>
				<Button size="sm" onclick={openGrantDialog} disabled={accessLoading}>
					<Plus class="h-4 w-4" />
					Grant Access
				</Button>
			</div>
		</Card.Action>
	</Card.Header>
	<Card.Content>
		{#if accessLoading}
			<TableSkeleton rows={3} columns={8} />
		{:else if usersWithAccess.length === 0}
			<p class="text-sm text-muted-foreground">
				No users have access. Click "Grant Access" to add users.
			</p>
		{:else}
			<div class="rounded-md border">
				<Table.Root>
					<Table.Header>
						<Table.Row>
							<Table.Head>User</Table.Head>
							{#each PERMISSION_KEYS as perm (perm)}
								<Table.Head class="min-w-[80px] px-4 text-center">
									<Tooltip.Root>
										<Tooltip.Trigger>
											{#snippet child({ props })}
												<span {...props} class="inline-flex cursor-default items-center gap-0.5">
													{perm}
													<HelpCircle class="h-3 w-3 opacity-40" />
												</span>
											{/snippet}
										</Tooltip.Trigger>
										<Tooltip.Content side="bottom">
											{PERMISSION_DESCRIPTIONS[perm] ?? perm}
										</Tooltip.Content>
									</Tooltip.Root>
								</Table.Head>
							{/each}
							<Table.Head class="w-[52px]"></Table.Head>
						</Table.Row>
					</Table.Header>
					<Table.Body>
						{#each usersWithAccess as username (username)}
							{@const entry = userPermMap[username]}
							<Table.Row>
								<Table.Cell class="font-medium">
									<a
										href="/users/{username}"
										class="text-primary underline-offset-4 hover:underline"
									>
										{username}
									</a>
								</Table.Cell>
								{#each PERMISSION_KEYS as perm (perm)}
									<Table.Cell class="px-4 text-center">
										<div class="flex justify-center">
											<Checkbox
												checked={entry?.permissions.has(perm) ?? false}
												onCheckedChange={() => toggleUserPerm(username, perm)}
												disabled={entry?.saving ?? false}
											/>
										</div>
									</Table.Cell>
								{/each}
								<Table.Cell>
									<div class="flex justify-center">
										<Button
											variant="ghost"
											size="icon"
											class="h-7 w-7"
											disabled={!entry?.dirty || entry?.saving}
											onclick={() => saveUserPerms(username)}
										>
											{#if entry?.saving}
												<Loader2 class="h-3.5 w-3.5 animate-spin" />
											{:else}
												<Save class="h-3.5 w-3.5" />
											{/if}
										</Button>
									</div>
								</Table.Cell>
							</Table.Row>
						{/each}
					</Table.Body>
				</Table.Root>
			</div>
		{/if}
	</Card.Content>
</Card.Root>

<!-- Grant Access Dialog -->
<Dialog.Root bind:open={grantOpen}>
	<Dialog.Content class="sm:max-w-lg">
		<Dialog.Header>
			<Dialog.Title>Grant Access</Dialog.Title>
			<Dialog.Description>
				Grant a user access to the "{namespaceName}" namespace.
			</Dialog.Description>
		</Dialog.Header>
		<form onsubmit={handleGrant} class="space-y-4">
			{#if grantError}
				<div
					class="rounded-lg border border-destructive/20 bg-destructive/10 p-3 text-sm text-destructive"
				>
					{grantError}
				</div>
			{/if}
			<div class="space-y-2">
				<Label for="grant-user">User</Label>
				<select
					id="grant-user"
					class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-9 w-full items-center rounded-md border px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
					bind:value={grantUser}
				>
					<option value="" disabled>Select a user...</option>
					{#each usersWithoutAccess as user (user.username)}
						<option value={user.username}>{user.username}</option>
					{/each}
				</select>
			</div>
			<div class="space-y-2">
				<Label>Permissions</Label>
				<div class="flex flex-wrap gap-4">
					{#each PERMISSION_KEYS as perm (perm)}
						<div class="flex items-center gap-2 text-sm">
							<Checkbox
								id="grant-{perm}"
								checked={grantPerms.has(perm)}
								onCheckedChange={() => toggleGrantPerm(perm)}
							/>
							<Label for="grant-{perm}">{perm}</Label>
							{#if PERMISSION_DESCRIPTIONS[perm]}
								<Tooltip.Root>
									<Tooltip.Trigger>
										{#snippet child({ props })}
											<span {...props}>
												<HelpCircle class="h-3 w-3 text-muted-foreground" />
											</span>
										{/snippet}
									</Tooltip.Trigger>
									<Tooltip.Content side="right">
										{PERMISSION_DESCRIPTIONS[perm]}
									</Tooltip.Content>
								</Tooltip.Root>
							{/if}
						</div>
					{/each}
				</div>
			</div>
			<Dialog.Footer>
				<Button variant="ghost" type="button" onclick={() => (grantOpen = false)}>Cancel</Button>
				<Button type="submit" disabled={granting || !grantUser || grantPerms.size === 0}>
					{granting ? 'Granting...' : 'Grant Access'}
				</Button>
			</Dialog.Footer>
		</form>
	</Dialog.Content>
</Dialog.Root>
