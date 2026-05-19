<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import type { ComponentProps } from 'svelte';
	import AppSidebar from './AppSidebar.svelte';

	type Args = ComponentProps<typeof AppSidebar>;

	const { Story } = defineMeta({
		title: 'Layout/AppSidebar',
		component: AppSidebar,
		parameters: {
			layout: 'fullscreen',
			sveltekit_experimental: {
				state: {
					page: {
						url: new URL('http://localhost/namespaces'),
						data: { tenant: 'dev-ai' },
					},
				},
			},
		},
	});
</script>

<script lang="ts">
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
</script>

<Story name="Sys Admin" args={{ accessLevel: 'sys-admin' }}>
	{#snippet template(args: Args)}
		<Sidebar.Provider>
			<AppSidebar {...args} />
		</Sidebar.Provider>
	{/snippet}
</Story>

<Story name="Tenant Admin" args={{ accessLevel: 'tenant-admin' }}>
	{#snippet template(args: Args)}
		<Sidebar.Provider>
			<AppSidebar {...args} />
		</Sidebar.Provider>
	{/snippet}
</Story>

<Story name="Namespace User" args={{ accessLevel: 'namespace-user' }}>
	{#snippet template(args: Args)}
		<Sidebar.Provider>
			<AppSidebar {...args} />
		</Sidebar.Provider>
	{/snippet}
</Story>
