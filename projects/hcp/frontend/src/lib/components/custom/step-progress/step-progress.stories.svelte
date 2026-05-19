<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import StepProgress from './step-progress.svelte';

	const { Story } = defineMeta({
		title: 'UI/StepProgress',
		component: StepProgress,
		args: {
			steps: [
				{ label: 'Validate configuration', status: 'pending' },
				{ label: 'Create namespace', status: 'pending' },
				{ label: 'Apply permissions', status: 'pending' },
			],
		},
	});
</script>

<Story
	name="All Pending"
	args={{
		steps: [
			{ label: 'Validate configuration', status: 'pending' },
			{ label: 'Create namespace', status: 'pending' },
			{ label: 'Apply permissions', status: 'pending' },
		],
	}}
/>

<Story
	name="In Progress"
	args={{
		steps: [
			{ label: 'Validate configuration', status: 'done' },
			{ label: 'Create namespace', status: 'running' },
			{ label: 'Apply permissions', status: 'pending' },
		],
	}}
/>

<Story
	name="All Done"
	args={{
		steps: [
			{ label: 'Validate configuration', status: 'done' },
			{ label: 'Create namespace', status: 'done' },
			{ label: 'Apply permissions', status: 'done' },
		],
	}}
/>

<Story
	name="With Error"
	args={{
		steps: [
			{ label: 'Validate configuration', status: 'done' },
			{ label: 'Create namespace', status: 'failed', error: 'Quota exceeded' },
			{ label: 'Apply permissions', status: 'pending' },
		],
	}}
/>

<Story
	name="With Groups"
	args={{
		steps: [
			{ label: 'production-data', group: 'Export', status: 'done' },
			{ label: 'staging-env', group: 'Export', status: 'done' },
			{ label: 'dev-sandbox', group: 'Export', status: 'running' },
			{ label: 'production-data', group: 'Import', status: 'pending' },
			{ label: 'staging-env', group: 'Import', status: 'pending' },
		],
	}}
/>
