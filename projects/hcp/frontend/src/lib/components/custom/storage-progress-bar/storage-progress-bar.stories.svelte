<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import StorageProgressBar from './storage-progress-bar.svelte';
	import type { ComponentProps } from 'svelte';

	type Args = ComponentProps<typeof StorageProgressBar>;

	const { Story } = defineMeta({
		title: 'UI/StorageProgressBar',
		component: StorageProgressBar,
		render: template,
		args: {
			percent: 50,
		},
		argTypes: {
			percent: { control: { type: 'range', min: 0, max: 150, step: 1 } },
		},
	});
</script>

{#snippet template(args: Args)}
	<div class="w-64">
		<StorageProgressBar {...args} />
		<p class="mt-1 text-xs text-muted-foreground">{args.percent}% used</p>
	</div>
{/snippet}

<Story name="Low Usage" args={{ percent: 30 }} />

<Story name="Medium Usage" args={{ percent: 65 }} />

<Story name="Warning" args={{ percent: 80 }} />

<Story name="Critical" args={{ percent: 95 }} />

<Story name="Empty" args={{ percent: 0 }} />

<Story name="Over Quota" args={{ percent: 120 }} />

<Story name="All Levels">
	{#snippet template()}
		<div class="space-y-4 w-64">
			{#each [0, 10, 30, 50, 70, 80, 90, 95, 100, 120] as pct}
				<div>
					<div class="flex justify-between text-xs text-muted-foreground mb-1">
						<span>{pct}%</span>
						<span
							>{pct > 100
								? 'Over quota'
								: pct > 90
									? 'Critical'
									: pct > 70
										? 'Warning'
										: 'OK'}</span
						>
					</div>
					<StorageProgressBar percent={pct} />
				</div>
			{/each}
		</div>
	{/snippet}
</Story>
