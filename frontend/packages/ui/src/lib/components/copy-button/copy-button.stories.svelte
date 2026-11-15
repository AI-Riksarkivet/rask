<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import CopyButton from './copy-button.svelte';

	const { Story } = defineMeta({
		title: 'Components/CopyButton',
		component: CopyButton,
		tags: ['autodocs'],
		args: { value: 'acme$bronze$orders', label: 'table id' },
	});
</script>

<Story name="Default" />
