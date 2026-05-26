<script context="module" lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { Dialog } from './index.js';
	import { Button } from '../button/index.js';

	const { Story } = defineMeta({
		title: 'Components/Dialog',
		tags: ['autodocs'],
	});
</script>

<Story name="Default">
	<Dialog.Root>
		<Dialog.Trigger>
			{#snippet child({ props })}
				<Button {...props}>Open dialog</Button>
			{/snippet}
		</Dialog.Trigger>
		<Dialog.Content>
			<Dialog.Title>Confirm action</Dialog.Title>
			<Dialog.Description>
				Are you sure you want to do this? It cannot be undone.
			</Dialog.Description>
		</Dialog.Content>
	</Dialog.Root>
</Story>
