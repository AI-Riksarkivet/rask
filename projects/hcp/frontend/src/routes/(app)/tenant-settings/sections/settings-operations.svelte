<script lang="ts">
	import { Switch } from '$lib/components/ui/switch/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Textarea } from '$lib/components/ui/textarea/index.js';
	import * as Card from '$lib/components/ui/card/index.js';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import SaveButton from '$lib/components/custom/save-button/save-button.svelte';
	import { useSave } from '$lib/utils/use-save.svelte.js';
	import { get_tenant, update_tenant, type TenantInfo } from '$lib/remote/tenant-info.remote.js';

	let {
		tenant,
	}: {
		tenant: string;
	} = $props();

	let tenantData = $derived(get_tenant({ tenant }));
	let info = $derived((tenantData?.current ?? {}) as TenantInfo & Record<string, unknown>);

	const saver = useSave({
		successMsg: 'Operational settings updated',
		errorMsg: 'Failed to update operational settings',
	});

	let localAdministrationAllowed = $state(true);
	let localMaxNamespacesPerUser = $state(100);
	let localSnmpLoggingEnabled = $state(false);
	let localSyslogLoggingEnabled = $state(false);
	let localTenantVisibleDescription = $state('');

	$effect(() => {
		const t = info;
		void saver.syncVersion;
		localAdministrationAllowed = (t.administrationAllowed as boolean) ?? true;
		localMaxNamespacesPerUser = (t.maxNamespacesPerUser as number) ?? 100;
		localSnmpLoggingEnabled = (t.snmpLoggingEnabled as boolean) ?? false;
		localSyslogLoggingEnabled = (t.syslogLoggingEnabled as boolean) ?? false;
		localTenantVisibleDescription = (t.tenantVisibleDescription as string) ?? '';
	});

	let dirty = $derived(
		localAdministrationAllowed !== ((info.administrationAllowed as boolean) ?? true) ||
			localMaxNamespacesPerUser !== ((info.maxNamespacesPerUser as number) ?? 100) ||
			localSnmpLoggingEnabled !== ((info.snmpLoggingEnabled as boolean) ?? false) ||
			localSyslogLoggingEnabled !== ((info.syslogLoggingEnabled as boolean) ?? false) ||
			localTenantVisibleDescription !== ((info.tenantVisibleDescription as string) ?? '')
	);
</script>

{#await tenantData}
	<CardSkeleton />
{:then}
	<Card.Root>
		<Card.Header>
			<Card.Title>Operations</Card.Title>
			<Card.Description>
				Tenant-level operational settings including logging, administration, and namespace limits
			</Card.Description>
		</Card.Header>
		<Card.Content>
			<div class="space-y-6">
				<div class="space-y-4">
					<h4 class="text-sm font-medium">Log Forwarding</h4>
					<p class="text-xs text-muted-foreground">
						Forward tenant log messages to the syslog servers and SNMP managers configured at the
						HCP system level. Destinations are managed by the system administrator.
					</p>
					<div class="flex flex-wrap gap-x-8 gap-y-4">
						<div class="space-y-1.5">
							<div class="flex items-center gap-2">
								<Switch id="syslog-logging" bind:checked={localSyslogLoggingEnabled} />
								<Label for="syslog-logging" class="text-sm">Syslog</Label>
							</div>
						</div>
						<div class="space-y-1.5">
							<div class="flex items-center gap-2">
								<Switch id="snmp-logging" bind:checked={localSnmpLoggingEnabled} />
								<Label for="snmp-logging" class="text-sm">SNMP</Label>
							</div>
						</div>
					</div>
				</div>

				<hr class="border-border" />

				<div class="space-y-4">
					<h4 class="text-sm font-medium">Administration</h4>
					<div class="space-y-1.5">
						<div class="flex items-center gap-2">
							<Switch id="administration-allowed" bind:checked={localAdministrationAllowed} />
							<Label for="administration-allowed" class="text-sm">
								Allow System-Level Administration
							</Label>
						</div>
						<p class="text-xs text-muted-foreground">
							Enables system-level administrative access to this tenant.
						</p>
					</div>
					<div class="space-y-2">
						<Label for="max-ns-per-user">Max Namespaces Per User</Label>
						<Input
							id="max-ns-per-user"
							type="number"
							min={0}
							max={10000}
							bind:value={localMaxNamespacesPerUser}
						/>
						<p class="text-xs text-muted-foreground">
							Maximum number of namespaces any single user can own (0–10,000).
						</p>
					</div>
				</div>

				<hr class="border-border" />

				<div class="space-y-2">
					<Label for="tenant-description">Tenant Description</Label>
					<Textarea
						id="tenant-description"
						bind:value={localTenantVisibleDescription}
						placeholder="Optional description visible to tenant users"
						rows={3}
					/>
					<p class="text-xs text-muted-foreground">
						A description visible to users of this tenant. Up to 1,024 characters.
					</p>
				</div>

				<SaveButton
					{dirty}
					saving={saver.saving}
					onclick={() =>
						saver.run(async () => {
							if (!tenantData) return;
							await update_tenant({
								tenant,
								body: {
									administrationAllowed: localAdministrationAllowed,
									maxNamespacesPerUser: localMaxNamespacesPerUser,
									snmpLoggingEnabled: localSnmpLoggingEnabled,
									syslogLoggingEnabled: localSyslogLoggingEnabled,
									tenantVisibleDescription: localTenantVisibleDescription,
								},
							}).updates(tenantData);
						})}
				/>
			</div>
		</Card.Content>
	</Card.Root>
{/await}
