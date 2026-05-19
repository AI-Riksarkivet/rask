<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import IpListEditor from './ip-list-editor.svelte';

	const { Story } = defineMeta({
		title: 'UI/IpListEditor',
		component: IpListEditor,
		args: {
			label: 'Allowed IPs',
			placeholder: 'IP address or CIDR (e.g. 10.0.0.0/8)',
			variant: 'secondary',
			emptyText: 'No addresses configured.',
			disabled: false,
		},
		argTypes: {
			variant: {
				control: 'select',
				options: ['secondary', 'destructive'],
			},
		},
	});
</script>

<script lang="ts">
	let emptyAddresses = $state<string[]>([]);
	let populatedAddresses = $state(['10.0.0.0/8', '192.168.1.0/24', '172.16.0.0/12']);
	let destructiveAddresses = $state(['10.0.0.1', '192.168.1.100']);
</script>

<Story name="Empty" args={{ label: 'Allowed IPs' }}>
	{#snippet template(args)}
		<IpListEditor
			bind:addresses={emptyAddresses}
			label={args.label}
			placeholder={args.placeholder}
			variant={args.variant}
			emptyText={args.emptyText}
			disabled={args.disabled}
		/>
	{/snippet}
</Story>

<Story name="With Addresses" args={{ label: 'Allow List' }}>
	{#snippet template(args)}
		<IpListEditor
			bind:addresses={populatedAddresses}
			label={args.label}
			placeholder={args.placeholder}
			variant={args.variant}
			emptyText={args.emptyText}
			disabled={args.disabled}
		/>
	{/snippet}
</Story>

<Story
	name="Destructive Variant"
	args={{
		label: 'Deny List',
		variant: 'destructive',
		placeholder: 'Block IP address...',
		emptyText: 'No blocked addresses.',
	}}
>
	{#snippet template(args)}
		<IpListEditor
			bind:addresses={destructiveAddresses}
			label={args.label}
			placeholder={args.placeholder}
			variant={args.variant}
			emptyText={args.emptyText}
			disabled={args.disabled}
		/>
	{/snippet}
</Story>

<Story name="Disabled" args={{ label: 'Read-only IPs', disabled: true }}>
	{#snippet template(args)}
		<IpListEditor
			addresses={['10.0.0.0/8', '192.168.1.0/24']}
			label={args.label}
			placeholder={args.placeholder}
			variant={args.variant}
			emptyText={args.emptyText}
			disabled={args.disabled}
		/>
	{/snippet}
</Story>
