<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import { ChevronRight } from 'lucide-svelte';
	import { rayJobs, type RayJobsPayload, type RayJob } from '$lib/api';
	import RayShell from '$lib/components/layout/ray-shell.svelte';
	import { SortHeader } from '@rask/ui/sort-header';
	import { Card } from '$lib/components/ui/card';
	import { Badge, type BadgeVariant } from '@rask/ui/badge';

	let payload = $state<RayJobsPayload | null>(null);
	let error = $state<string | null>(null);
	let timer: ReturnType<typeof setInterval> | null = null;
	let filter = $state<'all' | RayJob['status']>('all');
	let sortKey = $state('started');
	let sortDir = $state<'asc' | 'desc'>('desc');

	function setSort(col: string) {
		if (sortKey === col) sortDir = sortDir === 'asc' ? 'desc' : 'asc';
		else {
			sortKey = col;
			sortDir = col === 'started' ? 'desc' : 'asc';
		}
	}

	async function refresh() {
		try {
			payload = await rayJobs();
			error = null;
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
	}

	onMount(() => {
		refresh();
		timer = setInterval(refresh, 5000);
	});
	onDestroy(() => {
		if (timer) clearInterval(timer);
	});

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
		const f = filter === 'all' ? all : all.filter((j) => j.status === filter);
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

<svelte:head>
	<title>Jobs — RASK</title>
</svelte:head>

<RayShell title="Jobs">
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
							onclick={() => (filter = s as RayJob['status'])}
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
									onclick={() => goto(`/jobs/${encodeURIComponent(j.submission_id)}`)}
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
</RayShell>
