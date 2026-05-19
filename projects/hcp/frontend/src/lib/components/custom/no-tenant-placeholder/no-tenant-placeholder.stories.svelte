<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import NoTenantPlaceholder from './no-tenant-placeholder.svelte';

	const { Story } = defineMeta({
		title: 'UI/NoTenantPlaceholder',
		component: NoTenantPlaceholder,
		args: {
			message: 'No tenant selected. Please select a tenant to continue.',
		},
	});
</script>

<Story
	name="Default"
	args={{ message: 'No tenant selected. Please select a tenant to continue.' }}
/>

<Story
	name="Custom Message"
	args={{ message: 'You need administrator access to view this page.' }}
/>

<Story
	name="Long Message"
	args={{
		message:
			'This feature requires a valid tenant connection. Please navigate to the login page and authenticate with a tenant that has the appropriate permissions configured.',
	}}
/>
