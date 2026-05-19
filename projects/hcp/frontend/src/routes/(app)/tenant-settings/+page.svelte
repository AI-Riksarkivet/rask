<script lang="ts">
	import { page } from '$app/state';
	import * as Tabs from '$lib/components/ui/tabs/index.js';
	import {
		Settings,
		Contact,
		Database,
		Shield,
		Lock,
		Mail,
		Search,
		Globe,
		CreditCard,
		Wrench,
	} from 'lucide-svelte';
	import PageHeader from '$lib/components/custom/page-header/page-header.svelte';
	import NoTenantPlaceholder from '$lib/components/custom/no-tenant-placeholder/no-tenant-placeholder.svelte';
	import SettingsGeneral from './sections/settings-general.svelte';
	import SettingsContact from './sections/settings-contact.svelte';
	import SettingsNamespaceDefaults from './sections/settings-namespace-defaults.svelte';
	import SettingsPermissions from './sections/settings-permissions.svelte';
	import SettingsConsoleSecurity from './sections/settings-console-security.svelte';
	import SettingsEmailNotifications from './sections/settings-email-notifications.svelte';
	import SettingsSearchSecurity from './sections/settings-search-security.svelte';
	import SettingsCors from './sections/settings-cors.svelte';
	import SettingsServicePlans from './sections/settings-service-plans.svelte';
	import SettingsOperations from './sections/settings-operations.svelte';

	let tenant = $derived(page.data.tenant as string | undefined);
	let activeTab = $state('general');
</script>

<svelte:head>
	<title>Tenant Settings - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<PageHeader
		title="Tenant Settings"
		description="View and manage tenant configuration and permissions"
	/>

	{#if tenant}
		<Tabs.Root bind:value={activeTab}>
			<Tabs.List class="flex-wrap">
				<Tabs.Trigger value="general">
					<Settings class="mr-1.5 h-4 w-4" />
					General
				</Tabs.Trigger>
				<Tabs.Trigger value="contact">
					<Contact class="mr-1.5 h-4 w-4" />
					Contact
				</Tabs.Trigger>
				<Tabs.Trigger value="namespace-defaults">
					<Database class="mr-1.5 h-4 w-4" />
					Namespace Defaults
				</Tabs.Trigger>
				<Tabs.Trigger value="permissions">
					<Shield class="mr-1.5 h-4 w-4" />
					Permissions
				</Tabs.Trigger>
				<Tabs.Trigger value="console-security">
					<Lock class="mr-1.5 h-4 w-4" />
					Console Security
				</Tabs.Trigger>
				<Tabs.Trigger value="email">
					<Mail class="mr-1.5 h-4 w-4" />
					Email
				</Tabs.Trigger>
				<Tabs.Trigger value="search-security">
					<Search class="mr-1.5 h-4 w-4" />
					Search Security
				</Tabs.Trigger>
				<Tabs.Trigger value="cors">
					<Globe class="mr-1.5 h-4 w-4" />
					CORS
				</Tabs.Trigger>
				<Tabs.Trigger value="service-plans">
					<CreditCard class="mr-1.5 h-4 w-4" />
					Service Plans
				</Tabs.Trigger>
				<Tabs.Trigger value="operations">
					<Wrench class="mr-1.5 h-4 w-4" />
					Operations
				</Tabs.Trigger>
			</Tabs.List>

			<Tabs.Content value="general">
				<SettingsGeneral {tenant} />
			</Tabs.Content>

			<Tabs.Content value="contact">
				<SettingsContact {tenant} />
			</Tabs.Content>

			<Tabs.Content value="namespace-defaults">
				<SettingsNamespaceDefaults {tenant} />
			</Tabs.Content>

			<Tabs.Content value="permissions">
				<SettingsPermissions {tenant} />
			</Tabs.Content>

			<Tabs.Content value="console-security">
				<SettingsConsoleSecurity {tenant} />
			</Tabs.Content>

			<Tabs.Content value="email">
				<SettingsEmailNotifications {tenant} />
			</Tabs.Content>

			<Tabs.Content value="search-security">
				<SettingsSearchSecurity {tenant} />
			</Tabs.Content>

			<Tabs.Content value="cors">
				<SettingsCors {tenant} />
			</Tabs.Content>

			<Tabs.Content value="service-plans">
				<SettingsServicePlans {tenant} />
			</Tabs.Content>

			<Tabs.Content value="operations">
				<SettingsOperations {tenant} />
			</Tabs.Content>
		</Tabs.Root>
	{:else}
		<NoTenantPlaceholder message="Log in with a tenant to view settings." />
	{/if}
</div>
