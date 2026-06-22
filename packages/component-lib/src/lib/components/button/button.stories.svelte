<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import Button from './button.svelte';

	const { Story } = defineMeta({
		title: 'Components/Button',
		component: Button,
		tags: ['autodocs'],
		args: { variant: 'default', size: 'md' },
		argTypes: {
			variant: {
				control: 'select',
				options: ['default', 'destructive', 'outline', 'ghost', 'link'],
			},
			size: { control: 'select', options: ['sm', 'md', 'lg', 'icon'] },
			disabled: { control: 'boolean' },
		},
	});
</script>

<Story name="Default">
	{#snippet template(args)}
		<Button {...args}>Button</Button>
	{/snippet}
</Story>

<Story name="Outline" args={{ variant: 'outline' }}>
	{#snippet template(args)}
		<Button {...args}>Outline</Button>
	{/snippet}
</Story>

<Story name="Destructive" args={{ variant: 'destructive' }}>
	{#snippet template(args)}
		<Button {...args}>Destructive</Button>
	{/snippet}
</Story>

<Story name="Ghost" args={{ variant: 'ghost' }}>
	{#snippet template(args)}
		<Button {...args}>Ghost</Button>
	{/snippet}
</Story>

<Story name="Large" args={{ size: 'lg' }}>
	{#snippet template(args)}
		<Button {...args}>Large</Button>
	{/snippet}
</Story>

<Story name="Disabled" args={{ disabled: true }}>
	{#snippet template(args)}
		<Button {...args}>Disabled</Button>
	{/snippet}
</Story>
