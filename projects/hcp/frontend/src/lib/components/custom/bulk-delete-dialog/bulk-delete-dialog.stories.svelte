<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { fn } from 'storybook/test';
	import BulkDeleteDialog from './bulk-delete-dialog.svelte';

	const { Story } = defineMeta({
		title: 'UI/BulkDeleteDialog',
		component: BulkDeleteDialog,
		args: {
			open: true,
			count: 5,
			itemType: 'namespace',
			loading: false,
			onconfirm: fn(),
		},
	});
</script>

<Story name="Multiple Items" />

<Story name="Single Item" args={{ count: 1 }} />

<Story name="Loading" args={{ loading: true }} />

<Story name="Objects" args={{ count: 12, itemType: 'object' }} />

<Story
	name="Buckets with Force Option"
	args={{ count: 3, itemType: 'bucket', showForceOption: true }}
/>
