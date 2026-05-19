<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { fn } from 'storybook/test';
	import SaveButton from './save-button.svelte';

	const { Story } = defineMeta({
		title: 'UI/SaveButton',
		component: SaveButton,
		args: {
			onclick: fn(),
		},
	});
</script>

<Story name="Clean" args={{ dirty: false, saving: false }} />

<Story name="Dirty" args={{ dirty: true, saving: false }} />

<Story name="Saving" args={{ dirty: true, saving: true }} />
