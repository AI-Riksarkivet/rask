<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { submitChunk, syncBatches, type BatchRow, type ChunkRow } from '@rask/api';
	import { getBatches, getChunks, getRayJobs, getRayCluster } from '$lib/remote/overview.remote';
	import { goto } from '$app/navigation';
	import { Card } from '@rask/ui/card';
	import { Badge, type BadgeVariant } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { RefreshCw, Send } from '@lucide/svelte';

	// This app's base is /default/overview; in-app links use `base`. Cross-domain
	// links (discover/compute) stay project-prefixed — derive the project segment
	// from the base (there's no [project] route param in this carved app).
	const project = base.split('/')[1] ?? 'default';

	// THE ONE PATTERN (see lib/remote/overview.remote.ts):
	//  - Initial reads (batches, chunks) are SSR-rendered remote queries —
	//    `await`ed in markup (experimental.async), no onMount waterfall.
	//  - Live reads (ray jobs/cluster) are the SAME query objects, polled below
	//    via `.refresh().catch()` and read imperatively (`.current`) so refresh
	//    is flicker-free and a single 500 can't kill the poll loop.
	// Cached query handles (get_x() === get_x() while on the page).
	const batchesQuery = getBatches();
	const chunksQuery = getChunks();
	const rayJobsQuery = getRayJobs();
	const rayClusterQuery = getRayCluster();

	// SSR-rendered data — resolved on the server, hydrated, then kept fresh by
	// `runSync`/`handleSubmitChunk` refreshes. `await` is legal at the top level
	// of an async-mode component; first paint already has the data.
	const payload = $derived(await batchesQuery);
	const chunks = $derived(await chunksQuery);

	// Polled Ray reads — imperative `.current` (undefined until first resolve).
	// The dashboard proxy never 5xxs, so these resolve even when Ray is offline.
	const rayJobsPayload = $derived(rayJobsQuery.current ?? null);
	const rayClusterPayload = $derived(rayClusterQuery.current ?? null);

	let syncing = $state(false);

	let statusFilter = $state<'all' | string>('all');
	let manifestFilter = $state<'all' | string>('all');
	let chunkFilter = $state<'all' | number>('all');
	let search = $state('');
	let sortKey = $state<keyof BatchRow>('batch_id');
	let sortDir = $state<'asc' | 'desc'>('asc');

	let syncError = $state<string | null>(null);

	async function runSync() {
		syncing = true;
		syncError = null;
		try {
			// Mutation, then refetch the affected queries in place (flicker-free).
			await syncBatches();
			await Promise.all([batchesQuery.refresh(), chunksQuery.refresh()]);
		} catch (e: unknown) {
			syncError = e instanceof Error ? e.message : String(e);
		} finally {
			syncing = false;
		}
	}

	let submitting = $state<number | null>(null);
	let submitMsg = $state<string | null>(null);

	async function handleSubmitChunk(id: number) {
		submitting = id;
		submitMsg = null;
		try {
			const res = await submitChunk(id);
			submitMsg = `chunk ${id} submitted: ${res.stdout.split('\n').slice(-1)[0] ?? 'ok'}`;
			await Promise.all([rayJobsQuery.refresh(), rayClusterQuery.refresh()]);
		} catch (e: unknown) {
			submitMsg = e instanceof Error ? e.message : String(e);
		} finally {
			submitting = null;
		}
	}

	// Poll Ray every 5s. `.catch(() => {})` is mandatory: an uncaught refresh
	// rejection (one 500) evicts the query from cache and kills the loop.
	onMount(() => {
		const timer = setInterval(() => {
			rayJobsQuery.refresh().catch(() => {});
			rayClusterQuery.refresh().catch(() => {});
		}, 5000);
		return () => clearInterval(timer);
	});

	const totalExpected = $derived(payload.summary.accessible.expected);
	const totalCached = $derived(payload.summary.accessible.cached);
	const totalTranscribed = $derived(payload.summary.accessible.transcribed);
	const cachedPct = $derived(totalExpected ? (totalCached / totalExpected) * 100 : 0);
	const transcribedPct = $derived(totalExpected ? (totalTranscribed / totalExpected) * 100 : 0);
	const htrStatuses = $derived(Object.keys(payload.summary.by_htr_status).sort());
	const manifestStatuses = $derived(Object.keys(payload.summary.by_manifest_status).sort());

	const liveJobStatus = $derived.by(() => {
		const map = new Map<string, string>();
		const live = new Set(['PENDING', 'RUNNING']);
		for (const j of rayJobsPayload?.jobs ?? []) {
			if (live.has(j.status)) map.set(j.submission_id, j.status);
		}
		return map;
	});

	// Live prefetch jobs (submission_id 'prefetch-*') don't write to
	// current_rayjob_id, so we derive their batch coverage from /api/ray/jobs.
	const livePrefetchByBatch = $derived.by(() => {
		const map = new Map<string, string>();
		const live = new Set(['PENDING', 'RUNNING']);
		for (const j of rayJobsPayload?.jobs ?? []) {
			if (!j.submission_id?.startsWith('prefetch-')) continue;
			if (!live.has(j.status)) continue;
			for (const b of j.batches) map.set(b, j.status);
		}
		return map;
	});

	const runningChunks = $derived.by(() => {
		const out = new Set<number>();
		for (const b of payload.batches) {
			if (b.chunk_id !== null && b.current_rayjob_id && liveJobStatus.has(b.current_rayjob_id)) {
				out.add(b.chunk_id);
			}
		}
		return out;
	});

	const prefetchingChunks = $derived.by(() => {
		const out = new Set<number>();
		for (const b of payload.batches) {
			if (b.chunk_id !== null && livePrefetchByBatch.has(b.batch_id)) {
				out.add(b.chunk_id);
			}
		}
		return out;
	});

	const filtered = $derived.by(() => {
		const term = search.trim().toLowerCase();
		const out = payload.batches.filter((b) => {
			if (statusFilter !== 'all' && b.htr_status !== statusFilter) return false;
			if (manifestFilter !== 'all' && b.manifest_status !== manifestFilter) return false;
			if (chunkFilter !== 'all' && b.chunk_id !== chunkFilter) return false;
			if (term) {
				const hay = `${b.batch_id} ${b.arkiv_titel ?? ''} ${b.volym ?? ''}`.toLowerCase();
				if (!hay.includes(term)) return false;
			}
			return true;
		});
		const dir = sortDir === 'asc' ? 1 : -1;
		out.sort((a, b) => {
			const av = a[sortKey] ?? '';
			const bv = b[sortKey] ?? '';
			if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * dir;
			return String(av).localeCompare(String(bv)) * dir;
		});
		return out;
	});

	function chunkColor(c: ChunkRow): string {
		if (c.expected_pages === 0) return 'bg-muted';
		const tPct = c.transcribed_pages / c.expected_pages;
		const cPct = c.cached_pages / c.expected_pages;
		if (tPct >= 1) return 'bg-emerald-500';
		if (tPct > 0.5) return 'bg-emerald-500/70';
		if (tPct > 0) return 'bg-emerald-500/40';
		if (cPct >= 1) return 'bg-sky-500/70';
		if (cPct > 0) return 'bg-sky-500/40';
		return 'bg-muted';
	}

	function chunkTitle(c: ChunkRow): string {
		const tPct = c.expected_pages
			? ((c.transcribed_pages / c.expected_pages) * 100).toFixed(0)
			: '0';
		return (
			`chunk ${c.chunk_id} — ${c.batches} batches, ${c.expected_pages.toLocaleString()} pages\n` +
			`cached: ${c.cached_pages.toLocaleString()} · transcribed: ${c.transcribed_pages.toLocaleString()} (${tPct}%)\n` +
			`done batches: ${c.done_batches}/${c.batches}`
		);
	}

	function jobBadgeVariant(s: string): BadgeVariant {
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

	function statusBadgeVariant(s: string): BadgeVariant {
		switch (s) {
			case 'done':
				return 'success';
			case 'partial':
			case 'cached':
				return 'warning';
			case 'verification_failed':
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

	function setSort(k: keyof BatchRow) {
		if (sortKey === k) sortDir = sortDir === 'asc' ? 'desc' : 'asc';
		else {
			sortKey = k;
			sortDir = 'asc';
		}
	}

	function pct(num: number, den: number | null): string {
		if (!den) return '—';
		return `${((num / den) * 100).toFixed(0)}%`;
	}

	function fmtSync(iso: string | null): string {
		if (!iso) return 'never';
		const d = new Date(iso);
		const sameDay = d.toDateString() === new Date().toDateString();
		return sameDay
			? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
			: d.toLocaleString([], {
					month: 'short',
					day: 'numeric',
					hour: '2-digit',
					minute: '2-digit',
				});
	}
</script>

<svelte:head>
	<title>Overview — RASK</title>
</svelte:head>

<main class="bg-background flex-1 overflow-auto">
	<div class="flex flex-wrap items-center justify-end gap-2 px-6 pt-4">
		{#if payload}
			<span
				class="mr-1 hidden text-[11px] text-[oklch(0.78_0.005_260)] sm:inline"
				title={`last synced ${payload.generated_at ?? 'never'}`}
			>
				{payload.summary.total_batches.toLocaleString()} batches · synced {fmtSync(
					payload.generated_at,
				)}
			</span>
		{/if}
		<Button size="sm" variant="outline" onclick={runSync} disabled={syncing}>
			<RefreshCw class={`h-3.5 w-3.5 ${syncing ? 'animate-spin' : ''}`} />
			{syncing ? 'Syncing…' : 'Sync from S3'}
		</Button>
		<Button size="sm" onclick={() => goto(`${base}/new`)}>New volume</Button>
	</div>

	<div class="flex flex-col gap-4 p-6 text-sm">
		{#if syncError}
			<Card class="border-destructive/40 bg-destructive/10 text-destructive p-3">
				{syncError}
			</Card>
		{/if}

		<svelte:boundary>
			{#snippet pending()}
				<div class="text-muted-foreground">Loading…</div>
			{/snippet}

			{#snippet failed(boundaryError, reset)}
				<Card
					class="border-destructive/40 bg-destructive/10 text-destructive flex flex-col gap-2 p-3"
				>
					<span
						>{boundaryError instanceof Error ? boundaryError.message : String(boundaryError)}</span
					>
					<Button size="sm" variant="outline" onclick={reset}>Retry</Button>
				</Card>
			{/snippet}

			<!-- Summary tiles -->
			<section class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
				<Card class="p-4">
					<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
						Pages expected
					</div>
					<div class="mt-1 font-mono text-2xl tabular-nums">{totalExpected.toLocaleString()}</div>
					<div class="text-muted-foreground text-xs">
						{payload.summary.accessible.batches} accessible batches
					</div>
				</Card>
				<Card class="p-4">
					<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
						Cached in S3
					</div>
					<div class="mt-1 font-mono text-2xl tabular-nums">{totalCached.toLocaleString()}</div>
					<div class="bg-muted mt-1.5 h-1.5 w-full overflow-hidden rounded-full">
						<div class="h-full bg-sky-500 transition-all" style:width={`${cachedPct}%`}></div>
					</div>
					<div class="text-muted-foreground mt-1 text-xs">{cachedPct.toFixed(2)}%</div>
				</Card>
				<Card class="p-4">
					<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
						Transcribed
					</div>
					<div class="mt-1 font-mono text-2xl tabular-nums">
						{totalTranscribed.toLocaleString()}
					</div>
					<div class="bg-muted mt-1.5 h-1.5 w-full overflow-hidden rounded-full">
						<div
							class="h-full bg-emerald-500 transition-all"
							style:width={`${transcribedPct}%`}
						></div>
					</div>
					<div class="text-muted-foreground mt-1 text-xs">{transcribedPct.toFixed(2)}%</div>
				</Card>
				<Card class="p-4">
					<div class="flex items-baseline gap-2">
						<span class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
							Ray cluster
						</span>
						{#if rayClusterPayload?.ok}
							<Badge variant="success" class="text-[10px]">online</Badge>
						{:else}
							<Badge variant="secondary" class="text-[10px]">offline</Badge>
						{/if}
					</div>
					{#if rayClusterPayload?.ok && rayClusterPayload.total_resources}
						{@const tr = rayClusterPayload.total_resources}
						{@const ur = rayClusterPayload.used_resources!}
						<div class="mt-1 font-mono text-2xl tabular-nums">
							{ur.GPU.toFixed(0)}/{tr.GPU.toFixed(0)} GPU
						</div>
						<div class="text-muted-foreground text-xs">
							{ur.CPU.toFixed(0)}/{tr.CPU.toFixed(0)} CPU · {rayClusterPayload.alive_count}/{rayClusterPayload.node_count}
							nodes
						</div>
					{:else}
						<div class="text-muted-foreground mt-1 font-mono text-sm">no dashboard</div>
						<div
							class="text-muted-foreground truncate text-[10px]"
							title={rayClusterPayload?.error ?? rayJobsPayload?.error ?? ''}
						>
							{rayClusterPayload?.dashboard_url ?? ''}
						</div>
					{/if}
				</Card>
			</section>

			<!-- Recent RayJobs -->
			{#if rayJobsPayload?.ok && rayJobsPayload.jobs && rayJobsPayload.jobs.length}
				<Card class="overflow-hidden">
					<div class="flex items-center justify-between border-b px-4 py-2">
						<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
							Recent RayJobs
						</div>
						<a class="text-primary text-xs hover:underline" href={`/${project}/compute/jobs`}
							>view all →</a
						>
					</div>
					<div class="divide-border flex max-h-44 flex-col divide-y overflow-auto">
						{#each rayJobsPayload.jobs.slice(0, 10) as j (j.submission_id ?? j.job_id)}
							<div class="flex items-center gap-3 px-4 py-1.5 text-xs">
								<Badge
									variant={jobBadgeVariant(j.status)}
									class={`min-w-[68px] justify-center ${j.status === 'RUNNING' ? 'animate-pulse' : ''}`}
								>
									{j.status}
								</Badge>
								<span class="text-foreground font-mono"
									>{(j.submission_id ?? '—').slice(0, 24)}</span
								>
								<span class="text-muted-foreground">{fmtRuntime(j.start_time, j.end_time)}</span>
								{#if j.batches.length}
									<span class="text-muted-foreground truncate">
										{j.batches.length} batch{j.batches.length === 1 ? '' : 'es'}: {j.batches
											.slice(0, 3)
											.join(', ')}{j.batches.length > 3 ? ` +${j.batches.length - 3}` : ''}
									</span>
								{/if}
								{#if j.submission_id}
									<a
										class="text-primary ml-auto hover:underline"
										href={`/${project}/compute/jobs/${encodeURIComponent(j.submission_id)}`}
										title="Open job detail">details</a
									>
								{/if}
							</div>
						{/each}
					</div>
				</Card>
			{/if}

			<!-- Chunk strip -->
			{#if chunks.length}
				<Card class="p-4">
					<div class="mb-2 flex items-center justify-between gap-3">
						<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
							{chunks.length} chunks · ~{Math.round(chunks[0]!.expected_pages).toLocaleString()} pages
							each
						</div>
						{#if chunkFilter !== 'all'}
							<div class="flex items-center gap-2">
								<Button
									size="sm"
									onclick={() => handleSubmitChunk(chunkFilter as number)}
									disabled={submitting === chunkFilter}
								>
									<Send class="h-3.5 w-3.5" />
									{submitting === chunkFilter
										? `submitting chunk ${chunkFilter}…`
										: `submit chunk ${chunkFilter}`}
								</Button>
								<Button size="sm" variant="ghost" onclick={() => (chunkFilter = 'all')}
									>clear filter</Button
								>
							</div>
						{/if}
					</div>
					{#if submitMsg}
						<div class="border-primary/30 bg-primary/10 mb-2 rounded-md border px-3 py-1.5 text-xs">
							{submitMsg}
						</div>
					{/if}
					<div class="flex flex-wrap gap-[2px]">
						{#each chunks as c (c.chunk_id)}
							{@const isHtr = runningChunks.has(c.chunk_id)}
							{@const isPrefetch = prefetchingChunks.has(c.chunk_id) && !isHtr}
							<button
								type="button"
								class={`hover:ring-ring h-5 w-3 rounded-[2px] transition hover:ring-1 ${chunkColor(c)}
									${chunkFilter === c.chunk_id ? 'ring-primary ring-offset-card ring-2 ring-offset-1' : ''}
									${isHtr ? 'animate-pulse ring-2 ring-amber-400' : ''}
									${isPrefetch ? 'animate-pulse ring-2 ring-sky-400' : ''}`}
								title={`${chunkTitle(c)}${isHtr ? '\n[ray: htr in-flight]' : ''}${isPrefetch ? '\n[ray: prefetch in-flight]' : ''}`}
								onclick={() => (chunkFilter = chunkFilter === c.chunk_id ? 'all' : c.chunk_id)}
								aria-label={`chunk ${c.chunk_id}`}
							></button>
						{/each}
					</div>
				</Card>
			{/if}

			<!-- Filters -->
			<Card class="flex flex-wrap items-center gap-3 p-3">
				<input
					type="text"
					placeholder="Search batch_id, title, volym…"
					bind:value={search}
					class="bg-background focus-visible:ring-ring min-w-[14rem] flex-1 rounded-md border px-3 py-1.5 text-sm outline-none focus-visible:ring-2"
				/>
				<label class="text-muted-foreground flex items-center gap-2 text-xs">
					chunk
					<select
						bind:value={chunkFilter}
						class="bg-background rounded-md border px-2 py-1 text-sm"
					>
						<option value="all">all</option>
						{#each chunks as c (c.chunk_id)}
							<option value={c.chunk_id}>
								{c.chunk_id} ({c.batches}b · {c.transcribed_pages.toLocaleString()}/{c.expected_pages.toLocaleString()})
							</option>
						{/each}
					</select>
				</label>
				<label class="text-muted-foreground flex items-center gap-2 text-xs">
					HTR
					<select
						bind:value={statusFilter}
						class="bg-background rounded-md border px-2 py-1 text-sm"
					>
						<option value="all">all</option>
						{#each htrStatuses as s (s)}
							<option value={s}>{s} ({payload.summary.by_htr_status[s]})</option>
						{/each}
					</select>
				</label>
				<label class="text-muted-foreground flex items-center gap-2 text-xs">
					manifest
					<select
						bind:value={manifestFilter}
						class="bg-background rounded-md border px-2 py-1 text-sm"
					>
						<option value="all">all</option>
						{#each manifestStatuses as s (s)}
							<option value={s}>{s} ({payload.summary.by_manifest_status[s]})</option>
						{/each}
					</select>
				</label>
				<span class="text-muted-foreground ml-auto text-xs">
					{filtered.length.toLocaleString()} shown
				</span>
			</Card>

			<!-- Table -->
			<Card class="overflow-hidden">
				<div class="max-h-[60vh] overflow-auto">
					<table class="w-full border-collapse text-xs">
						<thead class="bg-card sticky top-0 z-10 text-left">
							<tr class="border-b">
								{#each [{ k: 'batch_id', label: 'batch_id' }, { k: 'arkiv_titel', label: 'titel' }, { k: 'volym', label: 'volym' }, { k: 'chunk_id', label: 'chunk' }, { k: 'page_count', label: 'pages' }, { k: 'cached_pages', label: 'cached' }, { k: 'transcribed_pages', label: 'transcribed' }, { k: 'htr_status', label: 'status' }, { k: 'current_rayjob_id', label: 'rayjob' }, { k: 'iiif_endpoint', label: 'iiif' }, { k: 'last_synced_at', label: 'synced' }] as col (col.k)}
									<th
										class="text-muted-foreground hover:bg-muted/50 cursor-pointer px-3 py-2 font-medium"
										onclick={() => setSort(col.k as keyof BatchRow)}
									>
										{col.label}
										{#if sortKey === col.k}
											<span class="text-foreground">{sortDir === 'asc' ? '↑' : '↓'}</span>
										{/if}
									</th>
								{/each}
							</tr>
						</thead>
						<tbody>
							{#each filtered as b (b.batch_id)}
								<tr class="border-border/40 hover:bg-muted/40 border-b">
									<td class="px-3 py-1.5 font-mono">
										<a
											class="text-primary hover:underline"
											href={`/${project}/discover/viewer/${b.batch_id}`}>{b.batch_id}</a
										>
									</td>
									<td class="max-w-[18rem] truncate px-3 py-1.5" title={b.arkiv_titel ?? ''}>
										{b.arkiv_titel ?? ''}
									</td>
									<td class="text-muted-foreground px-3 py-1.5">{b.volym ?? ''}</td>
									<td class="px-3 py-1.5 text-right font-mono">
										{#if b.chunk_id !== null}
											<button
												class="text-primary hover:underline"
												onclick={() => (chunkFilter = b.chunk_id!)}>{b.chunk_id}</button
											>
										{:else}—{/if}
									</td>
									<td class="px-3 py-1.5 text-right font-mono tabular-nums">
										{b.page_count ?? '—'}
									</td>
									<td class="px-3 py-1.5 text-right font-mono tabular-nums">
										{b.cached_pages || 0}
										<span class="text-muted-foreground">{pct(b.cached_pages, b.page_count)}</span>
									</td>
									<td class="px-3 py-1.5 text-right font-mono tabular-nums">
										{b.transcribed_pages || 0}
										<span class="text-muted-foreground"
											>{pct(b.transcribed_pages, b.page_count)}</span
										>
									</td>
									<td class="px-3 py-1.5">
										<Badge variant={statusBadgeVariant(b.htr_status)}>{b.htr_status}</Badge>
									</td>
									<td class="text-muted-foreground px-3 py-1.5 font-mono text-[10px]">
										{#if b.current_rayjob_id && liveJobStatus.has(b.current_rayjob_id)}
											<Badge variant="warning" class="animate-pulse">
												{liveJobStatus.get(b.current_rayjob_id)}
											</Badge>
											<span class="ml-1">{b.current_rayjob_id.slice(0, 16)}</span>
										{:else if livePrefetchByBatch.has(b.batch_id)}
											<Badge variant="warning" class="animate-pulse">
												prefetch {livePrefetchByBatch.get(b.batch_id)?.toLowerCase()}
											</Badge>
										{:else if b.current_rayjob_id}
											<span>{b.current_rayjob_id.slice(0, 16)}</span>
										{:else}—{/if}
									</td>
									<td class="text-muted-foreground px-3 py-1.5">{b.iiif_endpoint ?? ''}</td>
									<td class="text-muted-foreground px-3 py-1.5">
										{b.last_synced_at?.slice(0, 19) ?? ''}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</Card>
		</svelte:boundary>
	</div>
</main>
