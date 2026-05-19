<script lang="ts">
	import { Loader2, Search } from 'lucide-svelte';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Checkbox } from '$lib/components/ui/checkbox/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import * as Dialog from '$lib/components/ui/dialog/index.js';
	import { toast } from 'svelte-sonner';
	import {
		get_buckets,
		get_bucket_acl,
		put_bucket_acl,
		type AclData,
	} from '$lib/remote/buckets.remote.js';
	import { get_users, get_groups } from '$lib/remote/users.remote.js';
	import { get_namespaces, type Namespace } from '$lib/remote/namespaces.remote.js';
	import type { User, GroupAccount } from '$lib/constants.js';
	import { SvelteMap } from 'svelte/reactivity';
	import { ACL_PERMISSIONS, permissionLabel } from '../acl-constants.js';
	import { getErrorMessage } from '$lib/utils/get-error-message.js';

	interface Props {
		tenant?: string;
		open: boolean;
	}

	let { tenant, open = $bindable(false) }: Props = $props();

	let bucketData = get_buckets();
	let s3Buckets = $derived(
		(bucketData.current?.buckets ?? []) as { name: string; creation_date: string }[]
	);
	let nsData = $derived(tenant ? get_namespaces({ tenant }) : undefined);
	let namespaces = $derived((nsData?.current ?? []) as Namespace[]);

	// Merge S3 buckets with all accessible namespaces
	let buckets = $derived.by(() => {
		const s3Set = new Set(s3Buckets.map((b) => b.name));
		const merged = [...s3Buckets];
		for (const ns of namespaces) {
			if (!s3Set.has(ns.name)) {
				merged.push({ name: ns.name, creation_date: ns.creationTime ?? '' });
			}
		}
		return merged;
	});

	// Fetch ACL for each bucket
	let bucketAcls = $derived.by(() => {
		const map = new SvelteMap<string, ReturnType<typeof get_bucket_acl>>();
		for (const b of buckets) {
			map.set(b.name, get_bucket_acl({ bucket: b.name }));
		}
		return map;
	});

	// Fetch users and groups when tenant available
	let usersData = $derived(tenant && open ? get_users({ tenant }) : null);
	let groupsData = $derived(tenant && open ? get_groups({ tenant }) : null);
	let users = $derived((usersData?.current ?? []) as User[]);
	let groups = $derived((groupsData?.current ?? []) as GroupAccount[]);

	let granteeType = $state<'CanonicalUser' | 'Group'>('CanonicalUser');
	let granteeId = $state('');
	let grantPermission = $state('READ');
	let granting = $state(false);
	let grantBucketSelection = $state<Record<string, boolean>>({});
	let bucketSearch = $state('');

	// Reset state when dialog opens
	$effect(() => {
		if (open) {
			grantBucketSelection = {};
			granteeType = 'CanonicalUser';
			granteeId = '';
			grantPermission = 'READ';
			bucketSearch = '';
		}
	});

	let filteredBuckets = $derived(
		buckets.filter((b) => b.name.toLowerCase().includes(bucketSearch.toLowerCase()))
	);

	let grantBuckets = $derived(
		Object.entries(grantBucketSelection)
			.filter(([, v]) => v)
			.map(([name]) => name)
	);

	let allFilteredSelected = $derived(
		filteredBuckets.length > 0 && filteredBuckets.every((b) => grantBucketSelection[b.name])
	);

	function toggleAllFiltered(checked: boolean) {
		const next = { ...grantBucketSelection };
		for (const b of filteredBuckets) {
			if (checked) {
				next[b.name] = true;
			} else {
				delete next[b.name];
			}
		}
		grantBucketSelection = next;
	}

	async function handleGrant() {
		if (grantBuckets.length === 0 || !granteeId) return;
		granting = true;
		try {
			for (const bucketName of grantBuckets) {
				const aclQuery = bucketAcls.get(bucketName);
				const currentAcl = (aclQuery?.current ?? { owner: null, grants: [] }) as AclData;
				const newGrant = {
					Grantee: {
						Type: granteeType,
						...(granteeType === 'CanonicalUser' ? { ID: granteeId } : { URI: granteeId }),
					},
					Permission: grantPermission,
				};
				const result = put_bucket_acl({
					bucket: bucketName,
					owner: currentAcl.owner
						? { ID: currentAcl.owner.ID, DisplayName: currentAcl.owner.DisplayName }
						: undefined,
					grants: [
						...currentAcl.grants.map((g) => ({
							Grantee: g.Grantee,
							Permission: g.Permission,
						})),
						newGrant,
					],
				});
				if (aclQuery) await result.updates(aclQuery);
				else await result;
			}
			toast.success(
				`Granted ${permissionLabel(grantPermission)} to ${grantBuckets.length} bucket(s)`
			);
			open = false;
			granteeId = '';
			grantBucketSelection = {};
		} catch (err) {
			toast.error(getErrorMessage(err, 'Failed to grant access'));
		} finally {
			granting = false;
		}
	}
</script>

<Dialog.Root bind:open>
	<Dialog.Content class="sm:max-w-lg">
		<Dialog.Header>
			<Dialog.Title>Grant Access</Dialog.Title>
			<Dialog.Description>
				Add an ACL grant to one or more buckets. The new grant will be appended to each bucket's
				existing ACL.
			</Dialog.Description>
		</Dialog.Header>

		<div class="space-y-4">
			<!-- Bucket selection with search -->
			<div class="space-y-2">
				<Label>Buckets</Label>
				<div class="relative">
					<Search class="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
					<Input bind:value={bucketSearch} placeholder="Search buckets..." class="pl-10" />
				</div>
				<div class="max-h-60 space-y-1 overflow-y-auto rounded-md border p-3">
					<div class="mb-1 flex items-center gap-2 border-b pb-2">
						<Checkbox
							checked={allFilteredSelected}
							onCheckedChange={(val) => toggleAllFiltered(!!val)}
						/>
						<span class="text-sm font-medium">
							Select all{bucketSearch
								? ` matching (${filteredBuckets.length})`
								: ` (${buckets.length})`}
						</span>
					</div>
					{#each filteredBuckets as b (b.name)}
						<div class="flex items-center gap-2">
							<Checkbox
								checked={grantBucketSelection[b.name] ?? false}
								onCheckedChange={(val) => {
									grantBucketSelection = { ...grantBucketSelection, [b.name]: !!val };
								}}
							/>
							<span class="text-sm">{b.name}</span>
						</div>
					{/each}
					{#if filteredBuckets.length === 0}
						<p class="py-2 text-center text-sm text-muted-foreground">No buckets match</p>
					{/if}
				</div>
				{#if grantBuckets.length > 0}
					<p class="text-xs text-muted-foreground">
						{grantBuckets.length} bucket(s) selected
					</p>
				{/if}
			</div>

			<!-- Grantee Type -->
			<div class="space-y-2">
				<Label for="grantee-type">Grantee Type</Label>
				<select
					id="grantee-type"
					class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-9 w-full items-center rounded-md border px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
					bind:value={granteeType}
					onchange={() => {
						granteeId = '';
					}}
				>
					<option value="CanonicalUser">User</option>
					<option value="Group">Group</option>
				</select>
			</div>

			<!-- Grantee selector -->
			<div class="space-y-2">
				<Label>
					{granteeType === 'CanonicalUser' ? 'User' : 'Group'}
				</Label>
				{#if tenant && granteeType === 'CanonicalUser' && users.length > 0}
					<select
						class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-9 w-full items-center rounded-md border px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
						bind:value={granteeId}
					>
						<option value="">Select a user...</option>
						{#each users as u (u.username)}
							<option value={u.userGUID ?? u.username}>
								{u.username}{u.fullName ? ` — ${u.fullName}` : ''}
							</option>
						{/each}
					</select>
					{#if granteeId}
						<p class="truncate font-mono text-[11px] text-muted-foreground">
							ID: {granteeId}
						</p>
					{/if}
				{:else if tenant && granteeType === 'Group' && groups.length > 0}
					<select
						class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-9 w-full items-center rounded-md border px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
						bind:value={granteeId}
					>
						<option value="">Select a group...</option>
						{#each groups as g (g.groupname ?? g.name)}
							<option value={g.groupname ?? g.name ?? ''}>
								{g.groupname ?? g.name}{g.description ? ` — ${g.description}` : ''}
							</option>
						{/each}
					</select>
				{:else}
					<Input
						bind:value={granteeId}
						placeholder={granteeType === 'CanonicalUser'
							? 'HCP user canonical ID'
							: 'Group name or URI'}
					/>
				{/if}
			</div>

			<!-- Permission -->
			<div class="space-y-2">
				<Label for="grant-permission">Permission</Label>
				<select
					id="grant-permission"
					class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-9 w-full items-center rounded-md border px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
					bind:value={grantPermission}
				>
					{#each ACL_PERMISSIONS as p (p.value)}
						<option value={p.value}>{p.label}</option>
					{/each}
				</select>
			</div>
		</div>

		<Dialog.Footer>
			<Button variant="ghost" onclick={() => (open = false)} disabled={granting}>Cancel</Button>
			<Button onclick={handleGrant} disabled={granting || grantBuckets.length === 0 || !granteeId}>
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
