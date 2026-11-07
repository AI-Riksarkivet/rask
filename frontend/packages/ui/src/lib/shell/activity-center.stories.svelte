<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import ActivityCenter from './activity-center.svelte';
	import type { ActivityEntryLike } from './nav-config.js';

	// The bell, in its shapes. The panel is portalled, so open it to see the entries. A pinned clock
	// keeps the day headings stable, so a visual diff of this story is about the component rather than
	// about the clock.
	// No `component:` and no autodocs: each Story below supplies its own props.
	const { Story } = defineMeta({ title: 'Shell/ActivityCenter' });

	const NOW = Date.parse('2026-08-11T12:00:00Z');
	const ago = (minutes: number) => NOW - minutes * 60_000;

	const ENTRIES: ActivityEntryLike[] = [
		{
			id: 'a',
			label: 'Force-delete project acme',
			at: ago(2),
			ok: true,
		},
		{
			id: 'b',
			label: 'Delete project acme',
			at: ago(3),
			ok: false,
			status: 409,
			detail: "project 'acme' is protected against deletion. Pass force=true to override.",
		},
		{
			id: 'c',
			label: 'Replay lineage event run-7',
			at: ago(40),
			ok: false,
			status: 0,
			detail: 'TypeError: Failed to fetch',
		},
		{
			id: 'd',
			label: 'Undrop table bronze$events',
			at: ago(26 * 60),
			ok: true,
		},
	];
</script>

<!-- Two new since the panel was last opened; one entry from yesterday, so two day groups. -->
<Story name="With activity">
	<div class="flex justify-end p-8">
		<ActivityCenter entries={ENTRIES} unread={2} clock={() => NOW} />
	</div>
</Story>

<!-- Nothing written yet: no badge, and the panel says what will appear. -->
<Story name="Empty">
	<div class="flex justify-end p-8">
		<ActivityCenter entries={[]} unread={0} clock={() => NOW} />
	</div>
</Story>
