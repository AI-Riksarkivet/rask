<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import CopyableInput from './copyable-input.svelte';
	import * as Tooltip from '$lib/components/ui/tooltip/index.js';
	import type { ComponentProps } from 'svelte';

	type Args = ComponentProps<typeof CopyableInput>;

	const { Story } = defineMeta({
		title: 'UI/CopyableInput',
		component: CopyableInput,
		render: template,
		args: {
			value: 'urn:hcp:namespace:my-namespace',
			label: 'Canonical ID',
			secret: false,
		},
	});
</script>

{#snippet template(args: Args)}
	<Tooltip.Provider>
		<CopyableInput {...args} />
	</Tooltip.Provider>
{/snippet}

<Story name="Default" args={{ value: 'urn:hcp:namespace:my-namespace', label: 'Canonical ID' }} />

<Story
	name="Without Label"
	args={{ value: 'https://hcp.example.com/rest/namespaces', label: '' }}
/>

<Story
	name="Secret"
	args={{ value: 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY', label: 'Secret Key', secret: true }}
/>

<Story name="S3 Credentials">
	{#snippet template()}
		<Tooltip.Provider>
			<div class="space-y-3 max-w-md">
				<CopyableInput value="AKIAIOSFODNN7EXAMPLE" label="Access Key" />
				<CopyableInput value="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" label="Secret Key" secret />
				<CopyableInput value="https://s3.example.com" label="Endpoint" />
			</div>
		</Tooltip.Provider>
	{/snippet}
</Story>
