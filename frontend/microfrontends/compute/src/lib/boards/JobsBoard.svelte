<!--
	Jobs — the ZONE'S view, rendered by BOTH `/compute/jobs` and the dock's JobsPanel.

	It used to exist twice: the route, and a hand-written ~50-line table in the panel over the same
	remote. That is MIRRORING — the exact failure that retired the cross-zone compositor, reintroduced
	inside the zone where nothing was stopping it. The two drifted immediately: the page sorts,
	filters and links; the panel showed four columns and no controls.

	One component now. The panel is a sizing wrapper around it, so a column added here appears in both.
-->
<script lang="ts">
	import { liveRead } from '@rask/api/live';
	import { rayClock } from '$lib/live/ray-clock.svelte';
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import { ChevronRight } from '@lucide/svelte';
	import { type RayJob } from '@rask/api';
	import { getRayJobs } from '$lib/remote/compute.remote';
	import { SortHeader } from '@rask/ui/sort-header';
	import { Card } from '@rask/ui/card';
	import { page } from '$app/state';
	import { Badge, type BadgeVariant } from '@rask/ui/badge';

	// THE ONE PATTERN (see lib/remote/compute.remote.ts): a cached remote query,
	// polled below via `.refresh().catch()`, read imperatively (`.current`) for
	// flicker-free refresh. The dashboard proxy never 5xxs (offline-safe payload).
	const jobsQuery = getRayJobs();
	const payload = $derived(jobsQuery.current ?? null);
	const error = $derived(jobsQuery.error ? String(jobsQuery.error) : null);

	let filter = $state<'all' | RayJob['status']>('all');
	// THE TRANSFORM FILTER, seeded from `?transform=`. `$derived` rather than `$state` + an effect so the
	// board follows a navigation (a different transform's Runs link) instead of keeping the first value
	// it happened to load with; reassignable since 5.25, so the chip below can still clear it.
	let transformFilter = $derived(page.url.searchParams.get('transform') ?? '');
	let sortKey = $state('started');
	let sortDir = $state<'asc' | 'desc'>('desc');

	function setSort(col: string) {
		if (sortKey === col) sortDir = sortDir === 'asc' ? 'desc' : 'asc';
		else {
			sortKey = col;
			sortDir = col === 'started' ? 'desc' : 'asc';
		}
	}

	// THE ZONE'S ONE CLOCK. This used to be a private `setInterval` here, and in three sibling boards,
	// each re-reading the same shared no-arg cached queries on its own phase. `$lib/live/ray-clock`
	// holds the single interval and the POLL REASON for all of them; `liveRead` gives the
	// unconditional first read. `.catch(() => {})` stays mandatory per refresh — one uncaught
	// rejection evicts that query from cache and silently kills its updates, and it is the catch, not
	// a separate timer, that stops one failing query taking the others with it.
	$effect(() => rayClock.subscribe());
	liveRead(
		() => rayClock.cursor,
		() => {
			rayClock.refresh(jobsQuery);
		},
	);

	function jobVal(j: RayJob, key: string): string | number | null {
		switch (key) {
			case 'status':
				return j.status;
			case 'submission_id':
				return j.submission_id;
			case 'started':
				return j.start_time;
			case 'runtime':
				return j.start_time ? (j.end_time ?? Date.now()) - j.start_time : null;
			case 'batches':
				return j.batches.length;
			case 'message':
				return j.message;
			default:
				return null;
		}
	}

	const jobs = $derived.by(() => {
		const all = payload?.jobs ?? [];
		const byStatus = filter === 'all' ? all : all.filter((j) => j.status === filter);
		// A job carries its transform in Ray's own `metadata` (rask.transform, stamped by the medallion's
		// submit path). Jobs with no transform are EXCLUDED when a transform is being asked for — an
		// unstamped run is not "some transform's", it is nobody's.
		const f =
			transformFilter === ''
				? byStatus
				: byStatus.filter((j) => j.metadata?.['rask.transform'] === transformFilter);
		const dir = sortDir === 'asc' ? 1 : -1;
		return [...f].sort((x, y) => {
			const a = jobVal(x, sortKey);
			const b = jobVal(y, sortKey);
			if (a == null && b == null) return 0;
			if (a == null) return 1; // nulls always last
			if (b == null) return -1;
			const c =
				typeof a === 'number' && typeof b === 'number'
					? a - b
					: String(a).localeCompare(String(b), undefined, { numeric: true });
			return c * dir;
		});
	});

	const counts = $derived.by(() => {
		const c: Record<string, number> = {};
		for (const j of payload?.jobs ?? []) c[j.status] = (c[j.status] ?? 0) + 1;
		return c;
	});

	function variantFor(s: string): BadgeVariant {
		switch (s) {
			case 'SUCCEEDED':
				return 'success';
			case 'RUNNING':
			case 'PENDING':
				return 'warning';
			case 'FAILED':
				return 'destructive';
			default:
				return 'secondary';
		}
	}

	function fmtRuntime(start: number | null, end: number | null): string {
		if (!start) return '—';
		const endMs = end ?? Date.now();
		const secs = Math.max(0, (endMs - start) / 1000);
		if (secs < 90) return `${secs.toFixed(0)}s`;
		if (secs < 5400) return `${(secs / 60).toFixed(1)}m`;
		return `${(secs / 3600).toFixed(1)}h`;
	}

	function fmtTime(ts: number | null): string {
		if (!ts) return '—';
		return new Date(ts).toISOString().replace('T', ' ').slice(0, 19);
	}
</script>

<div class="bg-background h-full min-h-0 overflow-auto">
	<div class="flex flex-col gap-4 p-6 text-sm">
		{#if error}
			<Card class="border-destructive/40 bg-destructive/10 text-destructive p-3">{error}</Card>
		{/if}

		{#if payload && !payload.ok}
			<Card class="border-amber-500/40 bg-amber-500/10 p-3 text-sm">
				Ray dashboard unreachable at <span class="font-mono">{payload.dashboard_url}</span>
				{#if payload.error}
					<div class="text-muted-foreground mt-1 text-xs">{payload.error}</div>
				{/if}
			</Card>
		{/if}

		{#if payload?.ok}
			<section class="flex flex-wrap gap-2">
				<!-- A FILTER MUST BE VISIBLE. Arriving from a transform's Runs link silently shows a subset;
				     without this chip the board looks like the cluster has three jobs. Clicking it
				     clears the filter and drops the query param, so the URL and the view agree. -->
				{#if transformFilter}
					<button
						class="border-primary bg-primary/10 text-primary rounded-md border px-2.5 py-1 text-xs transition"
						onclick={() => {
							transformFilter = '';
							void goto(`${base}/jobs`, { replaceState: true, noScroll: true });
						}}
					>
						transform: {transformFilter} ✕
					</button>
				{/if}
				<button
					class={`rounded-md border px-2.5 py-1 text-xs transition ${filter === 'all' ? 'border-primary bg-primary/10 text-primary' : 'hover:bg-accent'}`}
					onclick={() => (filter = 'all')}
				>
					all <span class="text-muted-foreground">({(payload.jobs ?? []).length})</span>
				</button>
				{#each ['RUNNING', 'PENDING', 'SUCCEEDED', 'FAILED', 'STOPPED'] as s (s)}
					{#if counts[s]}
						<button
							class={`rounded-md border px-2.5 py-1 text-xs transition ${filter === s ? 'border-primary bg-primary/10 text-primary' : 'hover:bg-accent'}`}
							onclick={() => (filter = s)}
						>
							{s.toLowerCase()}
							<span class="text-muted-foreground">({counts[s]})</span>
						</button>
					{/if}
				{/each}
			</section>

			<Card class="overflow-hidden">
				<div class="max-h-[70vh] overflow-auto">
					<table class="w-full border-collapse text-xs">
						<thead class="bg-card sticky top-0 z-10 text-left">
							<tr class="border-b">
								{#each ['status', 'submission_id', 'started', 'runtime', 'batches', 'message'] as col (col)}
									<SortHeader label={col} {col} {sortKey} {sortDir} onsort={setSort} />
								{/each}
								<th class="px-3 py-2"></th>
							</tr>
						</thead>
						<tbody>
							{#each jobs as j (j.submission_id ?? j.job_id)}
								<tr
									class="border-border/40 hover:bg-muted/40 cursor-pointer border-b"
									onclick={() => goto(`${base}/jobs/${encodeURIComponent(j.submission_id)}`)}
								>
									<td class="px-3 py-1.5">
										<Badge
											variant={variantFor(j.status)}
											class={j.status === 'RUNNING' ? 'animate-pulse' : ''}>{j.status}</Badge
										>
									</td>
									<td class="px-3 py-1.5 font-mono">{j.submission_id}</td>
									<td class="text-muted-foreground px-3 py-1.5 font-mono">
										{fmtTime(j.start_time)}
									</td>
									<td class="px-3 py-1.5 font-mono tabular-nums">
										{fmtRuntime(j.start_time, j.end_time)}
									</td>
									<td class="text-muted-foreground px-3 py-1.5">
										{j.batches.length}
										{#if j.batches.length}
											<span class="ml-1 text-[10px]">
												{j.batches.slice(0, 2).join(', ')}{j.batches.length > 2
													? ` +${j.batches.length - 2}`
													: ''}
											</span>
										{/if}
									</td>
									<td
										class="text-muted-foreground max-w-[24rem] truncate px-3 py-1.5"
										title={j.message ?? ''}
									>
										{#if j.error_type}
											<span class="text-destructive">{j.error_type}</span>
										{/if}
										{j.message ?? ''}
									</td>
									<td class="px-3 py-1.5 text-right">
										<ChevronRight class="text-muted-foreground inline h-3.5 w-3.5" />
									</td>
								</tr>
							{/each}
							{#if jobs.length === 0}
								<tr>
									<td class="text-muted-foreground px-3 py-6 text-center" colspan="7">
										No jobs match this filter.
									</td>
								</tr>
							{/if}
						</tbody>
					</table>
				</div>
			</Card>
		{/if}
	</div>
</div>
