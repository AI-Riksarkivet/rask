<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import StatCard from './stat-card.svelte';
	const { Story } = defineMeta({
		title: 'UI/StatCard',
		component: StatCard,
		argTypes: {
			delay: {
				control: 'select',
				options: ['', 'delay-75', 'delay-150', 'delay-200'],
			},
		},
	});
</script>

<script lang="ts">
	import Database from 'lucide-svelte/icons/database';
	import HardDrive from 'lucide-svelte/icons/hard-drive';
	import Users from 'lucide-svelte/icons/users';
	import Shield from 'lucide-svelte/icons/shield';
	import StorageProgressBar from '$lib/components/custom/storage-progress-bar/storage-progress-bar.svelte';
</script>

<Story name="Default" args={{ label: 'Total Namespaces', value: '24' }}>
	{#snippet template(args)}
		<div class="w-72">
			<StatCard {...args} icon={Database} />
		</div>
	{/snippet}
</Story>

<Story name="With Children" args={{ label: 'Storage Used', value: '1.2 TB' }}>
	{#snippet template(args)}
		<div class="w-72">
			<StatCard {...args} icon={HardDrive}>
				<div class="mt-2">
					<StorageProgressBar percent={62} />
					<p class="mt-1 text-xs text-muted-foreground">62% of 2 TB quota</p>
				</div>
			</StatCard>
		</div>
	{/snippet}
</Story>

<Story name="Stats Grid">
	{#snippet template()}
		<div class="grid grid-cols-2 gap-4 lg:grid-cols-4">
			<StatCard label="Namespaces" value="24" icon={Database} />
			<StatCard label="Storage Used" value="1.2 TB" icon={HardDrive} delay="delay-75" />
			<StatCard label="Active Users" value="156" icon={Users} delay="delay-150" />
			<StatCard label="Access Policies" value="12" icon={Shield} delay="delay-200" />
		</div>
	{/snippet}
</Story>
