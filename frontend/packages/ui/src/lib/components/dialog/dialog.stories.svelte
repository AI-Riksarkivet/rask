<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { Dialog } from './index.js';

	const { Story } = defineMeta({
		title: 'Components/Dialog',
		tags: ['autodocs'],
	});
</script>

<!-- The @rask/ui Dialog.Trigger wraps Bits UI with `WithoutChild`, so it renders its
     own trigger button around `children` (no asChild/child snippet). -->
<Story name="Default">
	<Dialog.Root>
		<Dialog.Trigger
			class="bg-primary text-primary-foreground hover:bg-primary/90 inline-flex h-10 items-center rounded-md px-4 text-sm font-medium"
		>
			Open dialog
		</Dialog.Trigger>
		<Dialog.Content>
			<Dialog.Title>Confirm action</Dialog.Title>
			<Dialog.Description>Are you sure you want to do this? It cannot be undone.</Dialog.Description>
		</Dialog.Content>
	</Dialog.Root>
</Story>
