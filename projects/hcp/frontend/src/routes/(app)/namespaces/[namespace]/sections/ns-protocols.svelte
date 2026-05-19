<script lang="ts">
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
	import * as Card from '$lib/components/ui/card/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Switch } from '$lib/components/ui/switch/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { HelpCircle, Info, Terminal, Copy } from 'lucide-svelte';
	import SaveButton from '$lib/components/custom/save-button/save-button.svelte';
	import { useSave } from '$lib/utils/use-save.svelte.js';
	import { useCopyFeedback } from '$lib/utils/use-copy-feedback.svelte.js';
	import {
		get_ns_protocols,
		update_ns_protocol,
		type NsProtocols,
	} from '$lib/remote/namespaces.remote.js';

	let {
		tenant,
		namespaceName,
	}: {
		tenant: string;
		namespaceName: string;
	} = $props();

	const PROTOCOL_DESCRIPTIONS: Record<string, string> = {
		hs3Enabled: 'Enable S3-compatible API access (required for SDK and S3 tools)',
		restEnabled: 'Enable REST API access (HCP native HTTP)',
		httpEnabled: 'Allow unencrypted HTTP access',
		httpsEnabled: 'Allow encrypted HTTPS access',
		hswiftEnabled: 'Enable OpenStack Swift-compatible access',
		webdavEnabled: 'Enable WebDAV access for file browsing',
		cifsEnabled: 'Enable Windows file sharing (SMB/CIFS) access',
		nfsEnabled: 'Enable Unix/Linux NFS mount access',
		smtpEnabled: 'Enable email-based object ingestion',
	};

	// --- NFS connection info ---
	let nfsDomain = `nfs.<hcp-domain>`;
	let nfsMountPath = $derived(`/fs/${tenant}/${namespaceName}/data`);
	let nfsMountCommand = $derived(
		`mount -o tcp,vers=3,timeo=600,hard,intr -t nfs ${nfsDomain}:${nfsMountPath} /mnt/${namespaceName || 'hcp-data'}`
	);

	const nfsCopy = useCopyFeedback();
	function copyNfsCommand() {
		nfsCopy.copy(nfsMountCommand);
	}

	// --- Protocol state ---
	let protocolsData = $derived(get_ns_protocols({ tenant, name: namespaceName }));
	let protocols = $derived((protocolsData?.current ?? {}) as NsProtocols);

	const saver = useSave({
		successMsg: 'Protocols updated',
		errorMsg: 'Failed to update protocols',
	});

	let localHs3Enabled = $state(false);
	let localRestEnabled = $state(false);
	let localHttpEnabled = $state(false);
	let localHttpsEnabled = $state(false);
	let localHswiftEnabled = $state(false);
	let localWebdavEnabled = $state(false);
	let localCifsEnabled = $state(false);
	let localNfsEnabled = $state(false);
	let localSmtpEnabled = $state(false);

	$effect(() => {
		const p = protocols;
		void saver.syncVersion;
		localHs3Enabled = p.hs3Enabled ?? false;
		localRestEnabled = p.restEnabled ?? false;
		localHttpEnabled = p.httpEnabled ?? false;
		localHttpsEnabled = p.httpsEnabled ?? false;
		localHswiftEnabled = p.hswiftEnabled ?? false;
		localWebdavEnabled = p.webdavEnabled ?? false;
		localCifsEnabled = p.cifsEnabled ?? false;
		localNfsEnabled = p.nfsEnabled ?? false;
		localSmtpEnabled = p.smtpEnabled ?? false;
	});

	let dirty = $derived(
		localHs3Enabled !== (protocols.hs3Enabled ?? false) ||
			localRestEnabled !== (protocols.restEnabled ?? false) ||
			localHttpEnabled !== (protocols.httpEnabled ?? false) ||
			localHttpsEnabled !== (protocols.httpsEnabled ?? false) ||
			localHswiftEnabled !== (protocols.hswiftEnabled ?? false) ||
			localWebdavEnabled !== (protocols.webdavEnabled ?? false) ||
			localCifsEnabled !== (protocols.cifsEnabled ?? false) ||
			localNfsEnabled !== (protocols.nfsEnabled ?? false) ||
			localSmtpEnabled !== (protocols.smtpEnabled ?? false)
	);
</script>

{#snippet protoSwitch(
	id: string,
	label: string,
	checked: boolean,
	onChange: (v: boolean) => void,
	desc: string
)}
	<div class="flex items-center gap-2 text-sm">
		<Switch {id} {checked} onCheckedChange={onChange} />
		<Label for={id}>{label}</Label>
		<Tooltip.Root>
			<Tooltip.Trigger>
				{#snippet child({ props })}
					<span {...props}>
						<HelpCircle class="h-3 w-3 text-muted-foreground" />
					</span>
				{/snippet}
			</Tooltip.Trigger>
			<Tooltip.Content side="right">{desc}</Tooltip.Content>
		</Tooltip.Root>
	</div>
{/snippet}

<div class="flex h-full flex-col space-y-6">
	<Card.Root class="flex flex-1 flex-col">
		<Card.Header class="pb-3">
			<Card.Title class="text-base">Protocols</Card.Title>
			<Card.Description>
				Control which access protocols are enabled for this namespace.
			</Card.Description>
		</Card.Header>
		{#await protocolsData}
			<Card.Content>
				<div class="flex flex-wrap gap-4">
					{#each Array(9) as _, i (i)}
						<div class="h-5 w-20 animate-pulse rounded bg-muted"></div>
					{/each}
				</div>
			</Card.Content>
		{:then}
			<Card.Content class="space-y-4">
				<div>
					<p class="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
						Data Access
					</p>
					<div class="flex flex-wrap gap-x-6 gap-y-3">
						{@render protoSwitch(
							'proto-hs3',
							'S3 (HS3)',
							localHs3Enabled,
							(v) => (localHs3Enabled = v),
							PROTOCOL_DESCRIPTIONS.hs3Enabled
						)}
						{@render protoSwitch(
							'proto-rest',
							'REST',
							localRestEnabled,
							(v) => (localRestEnabled = v),
							PROTOCOL_DESCRIPTIONS.restEnabled
						)}
						{@render protoSwitch(
							'proto-hswift',
							'Swift',
							localHswiftEnabled,
							(v) => (localHswiftEnabled = v),
							PROTOCOL_DESCRIPTIONS.hswiftEnabled
						)}
						{@render protoSwitch(
							'proto-webdav',
							'WebDAV',
							localWebdavEnabled,
							(v) => (localWebdavEnabled = v),
							PROTOCOL_DESCRIPTIONS.webdavEnabled
						)}
					</div>
				</div>
				<div>
					<p class="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
						Transport
					</p>
					<div class="flex flex-wrap gap-x-6 gap-y-3">
						{@render protoSwitch(
							'proto-http',
							'HTTP',
							localHttpEnabled,
							(v) => (localHttpEnabled = v),
							PROTOCOL_DESCRIPTIONS.httpEnabled
						)}
						{@render protoSwitch(
							'proto-https',
							'HTTPS',
							localHttpsEnabled,
							(v) => (localHttpsEnabled = v),
							PROTOCOL_DESCRIPTIONS.httpsEnabled
						)}
					</div>
				</div>
				<div>
					<p class="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
						File System & Other
					</p>
					<div class="flex flex-wrap gap-x-6 gap-y-3">
						{@render protoSwitch(
							'proto-cifs',
							'CIFS',
							localCifsEnabled,
							(v) => (localCifsEnabled = v),
							PROTOCOL_DESCRIPTIONS.cifsEnabled
						)}
						{@render protoSwitch(
							'proto-nfs',
							'NFS',
							localNfsEnabled,
							(v) => (localNfsEnabled = v),
							PROTOCOL_DESCRIPTIONS.nfsEnabled
						)}
						{@render protoSwitch(
							'proto-smtp',
							'SMTP',
							localSmtpEnabled,
							(v) => (localSmtpEnabled = v),
							PROTOCOL_DESCRIPTIONS.smtpEnabled
						)}
					</div>
				</div>
			</Card.Content>
			<Card.Footer>
				<SaveButton
					{dirty}
					saving={saver.saving}
					onclick={() =>
						saver.run(async () => {
							if (!protocolsData) return;
							const changes: Array<{
								protocol: 'http' | 'cifs' | 'nfs' | 'smtp';
								body: Record<string, unknown>;
							}> = [];
							const httpChanged =
								localHttpEnabled !== (protocols.httpEnabled ?? false) ||
								localHttpsEnabled !== (protocols.httpsEnabled ?? false) ||
								localHs3Enabled !== (protocols.hs3Enabled ?? false) ||
								localRestEnabled !== (protocols.restEnabled ?? false) ||
								localHswiftEnabled !== (protocols.hswiftEnabled ?? false) ||
								localWebdavEnabled !== (protocols.webdavEnabled ?? false);
							if (httpChanged) {
								changes.push({
									protocol: 'http',
									body: {
										httpEnabled: localHttpEnabled,
										httpsEnabled: localHttpsEnabled,
										hs3Enabled: localHs3Enabled,
										restEnabled: localRestEnabled,
										hswiftEnabled: localHswiftEnabled,
										webdavEnabled: localWebdavEnabled,
									},
								});
							}
							if (localCifsEnabled !== (protocols.cifsEnabled ?? false)) {
								changes.push({ protocol: 'cifs', body: { cifsEnabled: localCifsEnabled } });
							}
							if (localNfsEnabled !== (protocols.nfsEnabled ?? false)) {
								changes.push({ protocol: 'nfs', body: { nfsEnabled: localNfsEnabled } });
							}
							if (localSmtpEnabled !== (protocols.smtpEnabled ?? false)) {
								changes.push({ protocol: 'smtp', body: { smtpEnabled: localSmtpEnabled } });
							}
							for (let i = 0; i < changes.length; i++) {
								const call = update_ns_protocol({
									tenant,
									name: namespaceName,
									protocol: changes[i].protocol,
									body: changes[i].body,
								});
								if (i === changes.length - 1) {
									await call.updates(protocolsData);
								} else {
									await call;
								}
							}
						})}
				/>
			</Card.Footer>
		{/await}
	</Card.Root>

	<!-- NFS Connection Instructions (shown when NFS is enabled) -->
	{#if localNfsEnabled}
		<Card.Root>
			<Card.Header class="pb-3">
				<Card.Title class="text-base">NFS Connection</Card.Title>
				<Card.Description>
					Mount instructions and connection details for NFS access to this namespace.
				</Card.Description>
			</Card.Header>
			<Card.Content class="space-y-4">
				<div
					class="flex items-start gap-3 rounded-md bg-blue-500/10 p-3 text-sm text-blue-700 dark:text-blue-300"
				>
					<Info class="mt-0.5 h-4 w-4 shrink-0" />
					<p>
						NFS uses <strong>IP-based access control</strong> — no username or password is needed. Make
						sure your client's IP is allowed.
					</p>
				</div>

				<div>
					<p class="mb-2 text-sm font-medium">Mount command</p>
					<div class="group relative">
						<pre class="overflow-x-auto rounded-md bg-muted p-3 text-sm"><code
								>{nfsMountCommand}</code
							></pre>
						<Button
							variant="ghost"
							size="icon"
							class="absolute right-2 top-2 h-7 w-7 opacity-0 transition-opacity group-hover:opacity-100"
							onclick={copyNfsCommand}
							title="Copy to clipboard"
						>
							{#if nfsCopy.copied}
								<span class="text-xs text-green-600">Copied!</span>
							{:else}
								<Copy class="h-4 w-4" />
							{/if}
						</Button>
					</div>
				</div>

				<div class="grid gap-4 sm:grid-cols-2">
					<div>
						<p class="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
							Mount data directory
						</p>
						<code class="text-sm">{nfsDomain}:{nfsMountPath}</code>
					</div>
					<div>
						<p class="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
							Mount metadata directory
						</p>
						<code class="text-sm">{nfsDomain}:/fs/{tenant}/{namespaceName}/metadata</code>
					</div>
				</div>

				<details class="text-sm">
					<summary class="cursor-pointer font-medium text-muted-foreground hover:text-foreground">
						<Terminal class="mr-1 inline h-4 w-4" /> Usage examples & tips
					</summary>
					<div class="mt-3 space-y-3 pl-1">
						<div>
							<p class="font-medium">Store an object</p>
							<pre class="mt-1 overflow-x-auto rounded-md bg-muted p-2"><code
									>cp myfile.txt /mnt/{namespaceName}/myfile.txt</code
								></pre>
						</div>
						<div>
							<p class="font-medium">Retrieve an object</p>
							<pre class="mt-1 overflow-x-auto rounded-md bg-muted p-2"><code
									>cp /mnt/{namespaceName}/myfile.txt ./local-copy.txt</code
								></pre>
						</div>
						<div>
							<p class="font-medium">Tips</p>
							<ul class="ml-4 mt-1 list-disc space-y-1 text-muted-foreground">
								<li>
									Do not specify <code>rsize</code> or <code>wsize</code> — HCP uses optimal values automatically
								</li>
								<li>Use <code>lookupcache=none</code> if you see stale file handle errors</li>
								<li>NFS uses lazy close — files are finalized after a short idle period</li>
								<li>Objects are immutable once closed (WORM) — you cannot overwrite or rename</li>
								<li>Multiple threads can read the same object on the same or different nodes</li>
							</ul>
						</div>
					</div>
				</details>
			</Card.Content>
		</Card.Root>
	{/if}
</div>
