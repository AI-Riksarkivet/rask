<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import ErrorBanner from './error-banner.svelte';

	const { Story } = defineMeta({
		title: 'UI/ErrorBanner',
		component: ErrorBanner,
		args: {
			message: 'Failed to save settings. Please try again.',
		},
	});
</script>

<Story name="With Message" args={{ message: 'Failed to save settings. Please try again.' }} />

<Story name="Empty" args={{ message: '' }} />

<Story
	name="Long Message"
	args={{
		message:
			'The namespace quota has been exceeded. Current usage is 150 GB which exceeds the hard quota of 100 GB. Please contact your administrator to increase the quota or delete unused objects.',
	}}
/>
