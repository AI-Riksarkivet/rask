<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import TestHarness from './namespace-permissions-editor-test-harness.svelte';

	const { Story } = defineMeta({
		title: 'UI/NamespacePermissionsEditor',
		component: TestHarness,
	});
</script>

<Story name="Default" />
