<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import * as Tabs from '$lib/components/ui/tabs/index.js';
	import { Switch } from '$lib/components/ui/switch/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import SaveButton from '$lib/components/custom/save-button/save-button.svelte';
	import IpListEditor from '$lib/components/custom/ip-list-editor/ip-list-editor.svelte';
	import { useSave } from '$lib/utils/use-save.svelte.js';
	import { arraysEqual } from '$lib/utils/format.js';
	import {
		get_ns_protocol_detail,
		update_ns_protocol,
		type IpSettings,
	} from '$lib/remote/namespaces.remote.js';

	const PROTOCOLS = ['http', 'nfs', 'cifs', 'smtp'] as const;
	type Protocol = (typeof PROTOCOLS)[number];

	const PROTOCOL_LABELS: Record<Protocol, string> = {
		http: 'HTTP / S3',
		nfs: 'NFS',
		cifs: 'CIFS',
		smtp: 'SMTP',
	};

	const PROTOCOL_DESCRIPTIONS: Record<Protocol, string> = {
		http: 'Controls which IP addresses can access this namespace via HTTP, REST, S3, and WebDAV.',
		nfs: 'Controls which IP addresses can mount this namespace via NFS. NFS uses IP-based access control only — no username/password.',
		cifs: 'Controls which IP addresses can access this namespace via CIFS/SMB file sharing.',
		smtp: 'Controls which IP addresses can send email to this namespace via SMTP.',
	};

	let {
		tenant,
		namespaceName,
	}: {
		tenant: string;
		namespaceName: string;
	} = $props();

	let activeProtocol = $state<Protocol>('http');

	// Fetch all protocol details
	let httpData = $derived(
		get_ns_protocol_detail({ tenant, name: namespaceName, protocol: 'http' })
	);
	let nfsData = $derived(get_ns_protocol_detail({ tenant, name: namespaceName, protocol: 'nfs' }));
	let cifsData = $derived(
		get_ns_protocol_detail({ tenant, name: namespaceName, protocol: 'cifs' })
	);
	let smtpData = $derived(
		get_ns_protocol_detail({ tenant, name: namespaceName, protocol: 'smtp' })
	);

	const queryDataMap = {
		get http() {
			return httpData;
		},
		get nfs() {
			return nfsData;
		},
		get cifs() {
			return cifsData;
		},
		get smtp() {
			return smtpData;
		},
	};

	function getIpSettings(proto: Protocol): IpSettings {
		return (queryDataMap[proto]?.current?.ipSettings ?? {}) as IpSettings;
	}

	// Per-protocol savers — created as a record to avoid repetition
	const savers: Record<Protocol, ReturnType<typeof useSave>> = {
		http: useSave({
			successMsg: 'HTTP network restrictions updated',
			errorMsg: 'Failed to update HTTP network restrictions',
		}),
		nfs: useSave({
			successMsg: 'NFS network restrictions updated',
			errorMsg: 'Failed to update NFS network restrictions',
		}),
		cifs: useSave({
			successMsg: 'CIFS network restrictions updated',
			errorMsg: 'Failed to update CIFS network restrictions',
		}),
		smtp: useSave({
			successMsg: 'SMTP network restrictions updated',
			errorMsg: 'Failed to update SMTP network restrictions',
		}),
	};

	// Per-protocol local state
	let localAllow = $state<Record<Protocol, string[]>>({ http: [], nfs: [], cifs: [], smtp: [] });
	let localDeny = $state<Record<Protocol, string[]>>({ http: [], nfs: [], cifs: [], smtp: [] });
	let localBoth = $state<Record<Protocol, boolean>>({
		http: false,
		nfs: false,
		cifs: false,
		smtp: false,
	});

	// Sync effects — one per protocol (required: each tracks its own reactive deps)
	$effect(() => {
		const s = getIpSettings('http');
		void savers.http.syncVersion;
		localAllow.http = [...(s.allowAddresses ?? [])];
		localDeny.http = [...(s.denyAddresses ?? [])];
		localBoth.http = s.allowIfInBothLists ?? false;
	});
	$effect(() => {
		const s = getIpSettings('nfs');
		void savers.nfs.syncVersion;
		localAllow.nfs = [...(s.allowAddresses ?? [])];
		localDeny.nfs = [...(s.denyAddresses ?? [])];
		localBoth.nfs = s.allowIfInBothLists ?? false;
	});
	$effect(() => {
		const s = getIpSettings('cifs');
		void savers.cifs.syncVersion;
		localAllow.cifs = [...(s.allowAddresses ?? [])];
		localDeny.cifs = [...(s.denyAddresses ?? [])];
		localBoth.cifs = s.allowIfInBothLists ?? false;
	});
	$effect(() => {
		const s = getIpSettings('smtp');
		void savers.smtp.syncVersion;
		localAllow.smtp = [...(s.allowAddresses ?? [])];
		localDeny.smtp = [...(s.denyAddresses ?? [])];
		localBoth.smtp = s.allowIfInBothLists ?? false;
	});

	function isDirty(proto: Protocol): boolean {
		const server = getIpSettings(proto);
		return (
			!arraysEqual(localAllow[proto], server.allowAddresses ?? []) ||
			!arraysEqual(localDeny[proto], server.denyAddresses ?? []) ||
			localBoth[proto] !== (server.allowIfInBothLists ?? false)
		);
	}

	function ruleCount(proto: Protocol): number {
		const s = getIpSettings(proto);
		return (s.allowAddresses?.length ?? 0) + (s.denyAddresses?.length ?? 0);
	}

	let httpDirty = $derived(isDirty('http'));
	let nfsDirty = $derived(isDirty('nfs'));
	let cifsDirty = $derived(isDirty('cifs'));
	let smtpDirty = $derived(isDirty('smtp'));
	const dirtyMap = {
		get http() {
			return httpDirty;
		},
		get nfs() {
			return nfsDirty;
		},
		get cifs() {
			return cifsDirty;
		},
		get smtp() {
			return smtpDirty;
		},
	};
</script>

{#snippet protocolEditor(proto: Protocol)}
	<IpListEditor
		bind:addresses={localAllow[proto]}
		label="Allow List"
		emptyText="No allow list configured. All addresses are allowed by default."
	/>
	<IpListEditor
		bind:addresses={localDeny[proto]}
		label="Deny List"
		placeholder="IP address or CIDR (e.g. 192.168.1.0/24)"
		variant="destructive"
		emptyText="No deny list configured."
	/>
	<div class="flex items-center gap-2">
		<Switch id="{proto}-allow-if-both" bind:checked={localBoth[proto]} />
		<Label for="{proto}-allow-if-both" class="text-sm">Allow if in both lists</Label>
	</div>
{/snippet}

{#await Promise.all([httpData, nfsData, cifsData, smtpData])}
	<CardSkeleton />
{:then}
	<Card.Root>
		<Card.Header>
			<Card.Title>Network Restrictions</Card.Title>
			<Card.Description>
				IP address allow and deny lists per protocol. Each protocol has its own independent
				restrictions.
			</Card.Description>
		</Card.Header>
		<Card.Content>
			<Tabs.Root bind:value={activeProtocol}>
				<Tabs.List>
					{#each PROTOCOLS as proto (proto)}
						{@const count = ruleCount(proto)}
						<Tabs.Trigger value={proto}>
							{PROTOCOL_LABELS[proto]}
							{#if count > 0}
								<Badge variant="secondary" class="ml-1.5 h-5 px-1.5 text-xs">
									{count}
								</Badge>
							{/if}
						</Tabs.Trigger>
					{/each}
				</Tabs.List>

				{#each PROTOCOLS as proto (proto)}
					<Tabs.Content value={proto} class="space-y-6 pt-4">
						<p class="text-sm text-muted-foreground">
							{PROTOCOL_DESCRIPTIONS[proto]}
						</p>

						{@render protocolEditor(proto)}

						<p class="text-xs text-muted-foreground">
							When "Allow if in both lists" is enabled, addresses that appear in both allow and deny
							lists will be allowed.
						</p>
					</Tabs.Content>
				{/each}
			</Tabs.Root>
		</Card.Content>
		<Card.Footer>
			<SaveButton
				dirty={dirtyMap[activeProtocol]}
				saving={savers[activeProtocol].saving}
				onclick={() => {
					const proto = activeProtocol;
					const qd = queryDataMap[proto];
					savers[proto].run(async () => {
						if (!qd) return;
						await update_ns_protocol({
							tenant,
							name: namespaceName,
							protocol: proto,
							body: {
								ipSettings: {
									allowAddresses: localAllow[proto].filter(Boolean),
									denyAddresses: localDeny[proto].filter(Boolean),
									allowIfInBothLists: localBoth[proto],
								},
							},
						}).updates(qd);
					});
				}}
			/>
		</Card.Footer>
	</Card.Root>
{/await}
