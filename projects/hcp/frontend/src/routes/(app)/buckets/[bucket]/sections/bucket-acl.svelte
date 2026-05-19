<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { Loader2, Shield, HelpCircle, Info, X, Plus, AlertTriangle } from 'lucide-svelte';
	import { toast } from 'svelte-sonner';
	import { SvelteMap } from 'svelte/reactivity';
	import {
		get_bucket_acl,
		put_bucket_acl,
		type AclGrant,
		type AclData,
	} from '$lib/remote/buckets.remote.js';
	import { get_users, get_groups } from '$lib/remote/users.remote.js';
	import type { User, GroupAccount } from '$lib/constants.js';
	import {
		ACL_PERMISSIONS,
		PERMISSION_MAP,
		permissionColor,
		permissionLabel,
	} from '../../../access-control/acl-constants.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	let {
		bucket,
		tenant,
	}: {
		bucket: string;
		tenant?: string;
	} = $props();

	let aclData = $derived(get_bucket_acl({ bucket }));
	let acl = $derived((aclData?.current ?? { owner: null, grants: [] }) as AclData);

	// Fetch users/groups when tenant is available
	let usersData = $derived(tenant ? get_users({ tenant }) : null);
	let groupsData = $derived(tenant ? get_groups({ tenant }) : null);
	let users = $derived((usersData?.current ?? []) as User[]);
	let groups = $derived((groupsData?.current ?? []) as GroupAccount[]);

	// Build a lookup map: userGUID -> username for display
	let userNameMap = $derived.by(() => {
		const map = new SvelteMap<string, string>();
		for (const u of users) {
			if (u.userGUID) map.set(u.userGUID, u.username);
			map.set(u.username, u.username);
		}
		return map;
	});

	// Add grant form state
	let granteeType = $state<'CanonicalUser' | 'Group'>('CanonicalUser');
	let granteeId = $state('');
	let grantPermission = $state('READ');
	let granting = $state(false);
	let revoking = $state('');

	// Grouped grants: merge permissions per grantee
	type GroupedGrant = {
		display: string;
		type: string;
		id: string;
		permissions: { value: string; grantIndex: number }[];
	};

	let groupedGrants = $derived.by((): GroupedGrant[] => {
		const map = new Map<string, GroupedGrant>();
		for (let i = 0; i < acl.grants.length; i++) {
			const grant = acl.grants[i];
			const id = getGranteeId(grant);
			let group = map.get(id);
			if (!group) {
				group = {
					display: getGranteeName(grant),
					type: getGranteeType(grant),
					id,
					permissions: [],
				};
				map.set(id, group);
			}
			group.permissions.push({ value: grant.Permission ?? '', grantIndex: i });
		}
		return [...map.values()];
	});

	function getGranteeName(grant: AclGrant): string {
		const g = grant.Grantee;
		if (!g) return 'Unknown';
		if (g.ID) {
			const resolved = userNameMap.get(g.ID as string);
			if (resolved) return resolved;
		}
		if (g.DisplayName) return g.DisplayName as string;
		if (g.ID) return (g.ID as string).slice(0, 16) + '…';
		if (g.URI) return (g.URI as string).split('/').pop() ?? (g.URI as string);
		return 'Unknown';
	}

	function getGranteeId(grant: AclGrant): string {
		const g = grant.Grantee;
		if (!g) return '';
		return (g.ID ?? g.URI ?? '') as string;
	}

	function getGranteeType(grant: AclGrant): string {
		const g = grant.Grantee;
		if (!g) return '';
		if (g.URI) return 'Group';
		return 'User';
	}

	async function addGrant() {
		if (!aclData || !granteeId) return;
		granting = true;
		try {
			const newGrant = {
				Grantee: {
					Type: granteeType,
					...(granteeType === 'CanonicalUser' ? { ID: granteeId } : { URI: granteeId }),
				},
				Permission: grantPermission,
			};
			await put_bucket_acl({
				bucket,
				owner: acl.owner ? { ID: acl.owner.ID, DisplayName: acl.owner.DisplayName } : undefined,
				grants: [
					...acl.grants.map((g) => ({ Grantee: g.Grantee, Permission: g.Permission })),
					newGrant,
				],
			}).updates(aclData);
			toast.success(`Granted ${permissionLabel(grantPermission)} access`);
			granteeId = '';
			grantPermission = 'READ';
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to add grant'));
		} finally {
			granting = false;
		}
	}

	async function revokePermission(grantIndex: number) {
		if (!aclData) return;
		const grant = acl.grants[grantIndex];
		const key = getGranteeId(grant) + ':' + grant.Permission;
		revoking = key;
		try {
			const remaining = acl.grants
				.filter((_, i) => i !== grantIndex)
				.map((g) => ({ Grantee: g.Grantee, Permission: g.Permission }));
			await put_bucket_acl({
				bucket,
				owner: acl.owner ? { ID: acl.owner.ID, DisplayName: acl.owner.DisplayName } : undefined,
				grants: remaining,
			}).updates(aclData);
			toast.success('Permission revoked');
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to revoke'));
		} finally {
			revoking = '';
		}
	}
</script>

<Card.Root>
	<Card.Header class="pb-3">
		<div class="flex items-center gap-2">
			<Shield class="h-4 w-4 text-muted-foreground" />
			<Card.Title class="text-base">Bucket ACL</Card.Title>
		</div>
		<Card.Description>
			S3 Access Control List — controls who can access this bucket and what they can do with it.
		</Card.Description>
	</Card.Header>
	{#await aclData}
		<Card.Content>
			<div class="space-y-2">
				{#each Array(2) as _, i (i)}
					<div class="h-5 w-48 animate-pulse rounded bg-muted"></div>
				{/each}
			</div>
		</Card.Content>
	{:then}
		<Card.Content class="space-y-4">
			{#if acl.error}
				<div
					class="flex items-start gap-2 rounded-md border border-amber-400 bg-amber-50 p-3 dark:bg-amber-950/30"
				>
					<AlertTriangle class="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
					<div class="text-sm text-amber-800 dark:text-amber-200">
						{acl.error}
					</div>
				</div>
			{:else}
				{#if acl.owner}
					<div class="flex items-center gap-1.5 text-sm text-muted-foreground">
						<span>Owner:</span>
						<span class="font-medium text-foreground"
							>{acl.owner.DisplayName || acl.owner.ID || '—'}</span
						>
						<Tooltip.Root>
							<Tooltip.Trigger>
								{#snippet child({ props })}
									<span {...props}>
										<HelpCircle class="h-3 w-3" />
									</span>
								{/snippet}
							</Tooltip.Trigger>
							<Tooltip.Content side="top" class="max-w-xs">
								The bucket owner always has full control regardless of the ACL grants below.
							</Tooltip.Content>
						</Tooltip.Root>
					</div>
				{/if}

				<details class="text-sm">
					<summary class="cursor-pointer font-medium text-muted-foreground hover:text-foreground">
						<Info class="mr-1 inline h-3.5 w-3.5" /> How ACLs work
					</summary>
					<div class="mt-2 space-y-1 rounded-md bg-muted/50 p-3 text-xs text-muted-foreground">
						<p>
							Each grant pairs a <strong class="text-foreground">grantee</strong> (user or group)
							with a <strong class="text-foreground">permission</strong>. The bucket owner always
							retains full control.
						</p>
						{#each ACL_PERMISSIONS as p (p.value)}
							<p><strong class="text-foreground">{p.label}</strong> — {p.description}</p>
						{/each}
					</div>
				</details>

				<!-- Compact add grant row -->
				<div class="flex flex-wrap items-end gap-2 rounded-md border bg-muted/30 p-3">
					<div class="space-y-1">
						<Label class="text-xs text-muted-foreground">Type</Label>
						<select
							class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-8 w-28 rounded-md border px-2 text-sm shadow-sm focus:outline-none focus:ring-1"
							bind:value={granteeType}
							onchange={() => {
								granteeId = '';
							}}
						>
							<option value="CanonicalUser">User</option>
							<option value="Group">Group</option>
						</select>
					</div>

					<div class="min-w-0 flex-1 space-y-1">
						<Label class="text-xs text-muted-foreground">
							{granteeType === 'CanonicalUser' ? 'User' : 'Group'}
						</Label>
						{#if tenant && granteeType === 'CanonicalUser' && users.length > 0}
							<select
								class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-8 w-full rounded-md border px-2 text-sm shadow-sm focus:outline-none focus:ring-1"
								bind:value={granteeId}
							>
								<option value="">Select a user…</option>
								{#each users as u (u.username)}
									<option value={u.userGUID ?? u.username}>
										{u.username}{u.fullName ? ` — ${u.fullName}` : ''}
									</option>
								{/each}
							</select>
						{:else if tenant && granteeType === 'Group' && groups.length > 0}
							<select
								class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-8 w-full rounded-md border px-2 text-sm shadow-sm focus:outline-none focus:ring-1"
								bind:value={granteeId}
							>
								<option value="">Select a group…</option>
								{#each groups as g (g.groupname ?? g.name)}
									<option value={g.groupname ?? g.name ?? ''}>
										{g.groupname ?? g.name}{g.description ? ` — ${g.description}` : ''}
									</option>
								{/each}
							</select>
						{:else}
							<input
								class="border-input bg-background text-foreground ring-offset-background placeholder:text-muted-foreground focus:ring-ring flex h-8 w-full rounded-md border px-2 text-sm shadow-sm focus:outline-none focus:ring-1"
								bind:value={granteeId}
								placeholder={granteeType === 'CanonicalUser'
									? 'User canonical ID'
									: 'Group name or URI'}
							/>
						{/if}
					</div>

					<div class="space-y-1">
						<Label class="text-xs text-muted-foreground">Permission</Label>
						<select
							class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-8 w-36 rounded-md border px-2 text-sm shadow-sm focus:outline-none focus:ring-1"
							bind:value={grantPermission}
						>
							{#each ACL_PERMISSIONS as p (p.value)}
								<option value={p.value}>{p.label}</option>
							{/each}
						</select>
					</div>

					<Button size="sm" class="h-8" disabled={granting || !granteeId} onclick={addGrant}>
						{#if granting}
							<Loader2 class="h-3.5 w-3.5 animate-spin" />
						{:else}
							<Plus class="h-3.5 w-3.5" /> Add
						{/if}
					</Button>
				</div>

				<!-- Current grants list -->
				{#if groupedGrants.length > 0}
					<div class="space-y-2">
						{#each groupedGrants as grantee (grantee.id)}
							<div class="rounded-md border px-3 py-2.5 text-sm">
								<div class="mb-1.5 flex items-center gap-1.5">
									<span class="font-medium">{grantee.display}</span>
									<Badge variant="outline" class="text-[10px]">{grantee.type}</Badge>
								</div>
								<div class="flex flex-wrap gap-1.5">
									{#each grantee.permissions as perm (perm.value)}
										{@const key = grantee.id + ':' + perm.value}
										<Tooltip.Root>
											<Tooltip.Trigger>
												{#snippet child({ props })}
													<span {...props} class="inline-flex items-center gap-0.5">
														<Badge variant={permissionColor(perm.value)} class="pr-1">
															{permissionLabel(perm.value)}
															<button
																class="ml-1 inline-flex h-3.5 w-3.5 items-center justify-center rounded-full hover:bg-black/20 dark:hover:bg-white/20"
																disabled={revoking === key}
																onclick={() => revokePermission(perm.grantIndex)}
																title="Revoke"
															>
																{#if revoking === key}
																	<Loader2 class="h-2.5 w-2.5 animate-spin" />
																{:else}
																	<X class="h-2.5 w-2.5" />
																{/if}
															</button>
														</Badge>
													</span>
												{/snippet}
											</Tooltip.Trigger>
											<Tooltip.Content side="top" class="max-w-xs">
												{PERMISSION_MAP.get(perm.value)?.description ?? perm.value}
											</Tooltip.Content>
										</Tooltip.Root>
									{/each}
								</div>
							</div>
						{/each}
					</div>
				{:else}
					<p
						class="rounded-md border border-dashed px-3 py-4 text-center text-sm text-muted-foreground"
					>
						No ACL grants configured. Only the bucket owner has access.
					</p>
				{/if}
			{/if}
		</Card.Content>
	{/await}
</Card.Root>
