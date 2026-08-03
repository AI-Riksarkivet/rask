<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { Textarea } from './index.js';

	const { Story } = defineMeta({
		title: 'Components/Textarea',
		component: Textarea,
		tags: ['autodocs'],
	});
</script>

<Story name="Default" args={{ placeholder: 'Say why the change is requested…' }} />

<Story name="Disabled" args={{ disabled: true, value: 'A disabled textarea' }} />
