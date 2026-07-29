<script lang="ts">
	import NotificationCenter from '../src/lib/shell/notification-center.svelte';
	import type { RunStatusLike } from '../src/lib/runs/run-status.js';

	// Live-shaped rows: copied from GET /lakehouse/api/runs as alice (2026-07-26), with a RUNNING
	// row carrying the progress facet and a FAIL row carrying an error, so every branch is on screen.
	const runs: RunStatusLike[] = [
		{
			run_id: '222ab538-0c97-52ae-9885-a862dde69fcb',
			job: 'lance-medallion/ingest_events',
			author: 'alice',
			state: 'START',
			outputs: [],
			updated_at: '2026-07-26T13:50:31.632953+00:00',
			events: 1,
			operation: 'ingest_events',
		},
		{
			run_id: '2b4dfa19-7713-5e30-a7bd-f0add13d1144',
			job: 'ray-jobs/embed_features',
			author: 'ray',
			state: 'RUNNING',
			progress_done: 2,
			progress_total: 5,
			outputs: ['silver$features'],
			updated_at: '2026-07-26T13:49:31.748288+00:00',
			events: 3,
			operation: 'embed_features',
		},
		{
			run_id: '415b71fc-14f5-5f7c-a0ea-cca607dddb1f',
			job: 'lance-medallion/aggregate_gold',
			author: 'analyst',
			state: 'COMPLETE',
			outputs: ['gold$catalog'],
			updated_at: '2026-07-26T13:48:31.855310+00:00',
			events: 4,
			operation: 'aggregate_gold',
		},
		{
			run_id: '88f4691a-4975-5938-9e87-3a49e8c528bb',
			job: 'lance-medallion/embed_features',
			author: 'data_eng',
			state: 'FAIL',
			error_message: 'ValueError: embedding dim 384 != table dim 768',
			outputs: [],
			updated_at: '2026-07-26T13:47:31.517648+00:00',
			events: 2,
			operation: 'embed_features',
		},
		{
			run_id: '6769edb4-66fe-5b4c-8ec0-553903502dc6',
			job: 'lance-medallion/lance_ray_ingest',
			author: 'ray',
			state: 'COMPLETE',
			outputs: ['raw_events'],
			updated_at: '2026-07-26T12:14:48.984361+00:00',
			events: 4,
			operation: 'lance_ray_ingest',
		},
	];

	let seen = $state<string[]>([]);
	let dismissed = $state<string[]>([]);
</script>

<div class="bg-background text-foreground flex h-svh flex-col">
	<div class="flex h-14 items-center gap-2 border-b px-4">
		<span class="text-sm font-medium">Lakehouse</span>
		<div class="ml-auto flex items-center gap-1">
			<NotificationCenter {runs} bind:seen bind:dismissed allHref="/lakehouse/lineage/runs" />
			<div class="bg-muted flex size-8 items-center justify-center rounded-full text-xs">AL</div>
		</div>
	</div>
	<p class="text-muted-foreground p-4 text-xs">
		seen: {seen.length} · dismissed: {dismissed.length}
	</p>
</div>
