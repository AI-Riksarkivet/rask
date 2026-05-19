<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import type { ComponentProps } from 'svelte';
	import BackButton from './back-button.svelte';

	type Args = ComponentProps<typeof BackButton>;

	const { Story } = defineMeta({
		title: 'UI/BackButton',
		component: BackButton,
		args: {
			href: '/namespaces',
			label: 'Back to Namespaces',
		},
	});
</script>

<script lang="ts">
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
</script>

<Story name="Default" args={{ href: '/namespaces', label: 'Back to Namespaces' }}>
	{#snippet template(args: Args)}
		<Tooltip.Provider>
			<BackButton {...args} />
		</Tooltip.Provider>
	{/snippet}
</Story>

<Story name="Back to Users" args={{ href: '/users', label: 'Back to Users' }}>
	{#snippet template(args: Args)}
		<Tooltip.Provider>
			<BackButton {...args} />
		</Tooltip.Provider>
	{/snippet}
</Story>

<Story name="Back to Buckets" args={{ href: '/buckets', label: 'Back to Buckets' }}>
	{#snippet template(args: Args)}
		<Tooltip.Provider>
			<BackButton {...args} />
		</Tooltip.Provider>
	{/snippet}
</Story>
