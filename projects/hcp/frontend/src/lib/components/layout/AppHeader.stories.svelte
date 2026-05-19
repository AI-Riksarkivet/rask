<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import type { ComponentProps } from 'svelte';
	import AppHeader from './AppHeader.svelte';

	type Args = ComponentProps<typeof AppHeader>;

	const now = Math.floor(Date.now() / 1000);
	const future = now + 60 * 60 * 4;

	const { Story } = defineMeta({
		title: 'Layout/AppHeader',
		component: AppHeader,
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

<Story
	name="Basic"
	args={{
		username: 'admin',
		tenant: 'dev-ai',
	}}
>
	{#snippet template(args: Args)}
		<Sidebar.Provider>
			<AppHeader {...args} />
		</Sidebar.Provider>
	{/snippet}
</Story>

<Story
	name="With GUID"
	args={{
		username: 'admin',
		tenant: 'dev-ai',
		userGUID: 'a1b2c3d4-e5f6-7890-abcd-ef1234567890',
	}}
>
	{#snippet template(args: Args)}
		<Sidebar.Provider>
			<AppHeader {...args} />
		</Sidebar.Provider>
	{/snippet}
</Story>

<Story
	name="With Sessions"
	args={{
		username: 'admin',
		tenant: 'dev-ai',
		userGUID: 'a1b2c3d4-e5f6-7890-abcd-ef1234567890',
		sessions: [
			{
				tenant: 'dev-ai',
				username: 'admin',
				exp: future,
				expired: false,
				cookieName: 'hcp_token__dev-ai__admin',
				isActive: true,
			},
			{
				tenant: 'prod-ai',
				username: 'admin',
				exp: future,
				expired: false,
				cookieName: 'hcp_token__prod-ai__admin',
				isActive: false,
			},
		],
	}}
>
	{#snippet template(args: Args)}
		<Sidebar.Provider>
			<AppHeader {...args} />
		</Sidebar.Provider>
	{/snippet}
</Story>

<Story
	name="No Tenant"
	args={{
		username: 'sysadmin',
	}}
>
	{#snippet template(args: Args)}
		<Sidebar.Provider>
			<AppHeader {...args} />
		</Sidebar.Provider>
	{/snippet}
</Story>
