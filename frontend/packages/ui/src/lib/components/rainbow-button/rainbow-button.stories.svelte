<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import RainbowButton from './rainbow-button.svelte';

	const { Story } = defineMeta({
		title: 'Components/RainbowButton',
		component: RainbowButton,
	});
</script>

<!-- The estate's ONE loud affordance — entry points that summon a model. Anything quieter is a
     plain Button; if two of these ever sit on one screen, one of them is wrong. -->
<Story name="Default">
	<RainbowButton>✦ AI assist</RainbowButton>
</Story>
