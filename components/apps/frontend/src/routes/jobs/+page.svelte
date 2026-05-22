<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { rayJobs, type RayJobsPayload, type RayJob } from '$lib/api';
	import RayShell from '$lib/components/layout/ray-shell.svelte';
	import { Card } from '$lib/components/ui/card';
	import { Badge, type BadgeVariant } from '$lib/components/ui/badge';

	let payload = $state<RayJobsPayload | null>(null);
	let error = $state<string | null>(null);
	let timer: ReturnType<typeof setInterval> | null = null;
	let filter = $state<'all' | RayJob['status']>('all');

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

	const jobs = $derived.by(() => {
		const all = payload?.jobs ?? [];
		const f = filter === 'all' ? all : all.filter((j) => j.status === filter);
		return [...f].sort((a, b) => (b.start_time ?? 0) - (a.start_time ?? 0));
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
		const endTs = end ?? Date.now() / 1000;
		const secs = Math.max(0, endTs - start);
		if (secs < 90) return `${secs.toFixed(0)}s`;
		if (secs < 5400) return `${(secs / 60).toFixed(1)}m`;
		return `${(secs / 3600).toFixed(1)}h`;
	}

	function fmtTime(ts: number | null): string {
		if (!ts) return '—';
		return new Date(ts * 1000).toISOString().replace('T', ' ').slice(0, 19);
	}
</script>

<svelte:head>
	<title>Jobs — RASK</title>
</svelte:head>

<RayShell title="Jobs">
	<div class="flex flex-col gap-4 p-6 text-sm">
		{#if error}
			<Card class="border-destructive/40 bg-destructive/10 p-3 text-destructive">{error}</Card>
		{/if}

		{#if payload && !payload.ok}
			<Card class="border-amber-500/40 bg-amber-500/10 p-3 text-sm">
				Ray dashboard unreachable at <span class="font-mono">{payload.dashboard_url}</span>
				{#if payload.error}
					<div class="mt-1 text-xs text-muted-foreground">{payload.error}</div>
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
						<thead class="sticky top-0 z-10 bg-card text-left">
							<tr class="border-b">
								{#each ['status', 'submission_id', 'started', 'runtime', 'batches', 'message'] as col (col)}
									<th class="px-3 py-2 font-medium text-muted-foreground">{col}</th>
								{/each}
								<th class="px-3 py-2 font-medium text-muted-foreground">logs</th>
							</tr>
						</thead>
						<tbody>
							{#each jobs as j (j.submission_id)}
								<tr class="border-b border-border/40 hover:bg-muted/40">
									<td class="px-3 py-1.5">
										<Badge
											variant={variantFor(j.status)}
											class={j.status === 'RUNNING' ? 'animate-pulse' : ''}
										>{j.status}</Badge>
									</td>
									<td class="px-3 py-1.5 font-mono">{j.submission_id}</td>
									<td class="px-3 py-1.5 font-mono text-muted-foreground">
										{fmtTime(j.start_time)}
									</td>
									<td class="px-3 py-1.5 font-mono tabular-nums">
										{fmtRuntime(j.start_time, j.end_time)}
									</td>
									<td class="px-3 py-1.5 text-muted-foreground">
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
										class="max-w-[24rem] truncate px-3 py-1.5 text-muted-foreground"
										title={j.message ?? ''}
									>
										{#if j.error_type}
											<span class="text-destructive">{j.error_type}</span>
										{/if}
										{j.message ?? ''}
									</td>
									<td class="px-3 py-1.5">
										{#if j.submission_id}
											<a
												href={`/embed?path=/jobs/${encodeURIComponent(j.submission_id)}`}
												class="text-primary hover:underline"
												title="Open in embedded Ray dashboard">logs</a
											>
										{:else}—{/if}
									</td>
								</tr>
							{/each}
							{#if jobs.length === 0}
								<tr>
									<td class="px-3 py-6 text-center text-muted-foreground" colspan="7">
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
