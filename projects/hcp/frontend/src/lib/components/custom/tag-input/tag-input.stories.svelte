<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import TagInput from './tag-input.svelte';
	const { Story } = defineMeta({
		title: 'UI/TagInput',
		component: TagInput,
	});
</script>

<Story name="Empty" args={{ tags: [], placeholder: 'Add tag...' }}>
	{#snippet template(args)}
		<div class="max-w-md">
			<TagInput {...args} />
		</div>
	{/snippet}
</Story>

<Story name="With Tags" args={{ tags: ['production', 'critical', 'us-east-1'] }}>
	{#snippet template(args)}
		<div class="max-w-md">
			<TagInput {...args} />
		</div>
	{/snippet}
</Story>

<Story name="Disabled" args={{ tags: ['locked', 'readonly'], disabled: true }}>
	{#snippet template(args)}
		<div class="max-w-md">
			<TagInput {...args} />
		</div>
	{/snippet}
</Story>

<Story
	name="Many Tags"
	args={{
		tags: [
			'production',
			'critical',
			'us-east-1',
			'team-alpha',
			'compliance',
			'backup',
			'encrypted',
			'high-priority',
		],
	}}
>
	{#snippet template(args)}
		<div class="max-w-md">
			<TagInput {...args} />
		</div>
	{/snippet}
</Story>
