<!-- The app build (and svelte-check) compiles this WITHOUT customElement: true — correct, the app
     never mounts the wrapper; only vite.elements.config.ts compiles it as an element. -->
<!-- svelte-ignore options_missing_custom_element -->
<svelte:options customElement={{ shadow: 'none' }} />

<script lang="ts">
	/**
	 * `<rask-compute-actors>` — live Ray actors, VISUALLY IDENTICAL to the zone's /compute/actors
	 * table: same Card, same Badge variants, same icon tiles, same expand-row detail grid, same
	 * utility classes (compiled into this bundle by elements.css — the host cannot generate them).
	 * The mount stamp + poll counter stay as the no-remount witness; rows dispatch the rask:select
	 * contract event (and toggle their detail row) instead of navigating.
	 *
	 * Two page affordances are deliberately NOT mirrored: the summary strip / filter controls (page
	 * chrome) and the node hostname lookup — the zone joins /api/ray/cluster for it, which this
	 * single-poll panel does not read, so `nodeLabel` degrades to the ip / node_id the actor itself
	 * carries rather than inventing a name.
	 */
	import { Badge } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import { actorsList, type ActorInfo } from '@rask/api';
	import { RASK_SELECT, type SelectDetail } from '@rask/dockview/contract';
	import { RayPoll } from './ray-poll.svelte';
	import {
		Boxes,
		ServerCog,
		Network,
		Gauge,
		ListTree,
		Cpu,
		TriangleAlert,
		ChevronRight,
	} from '@lucide/svelte';

	let { pollms = 5000, filtertext = '' }: { pollms?: number; filtertext?: string } = $props();
	const poll = new RayPoll<ActorInfo[]>((f) => actorsList(f));
	$effect(() => poll.start(pollms));

	const actors = $derived(poll.data ?? []);
	/** The cross-filter (wave 3): the compositor pushes the active selection's label down as a
	 *  property; rows narrow by substring over their serialized form — generic on purpose, every
	 *  list element filters the same way. */
	const shownactors = $derived(
		filtertext.trim() === ''
			? actors
			: actors.filter((r) => JSON.stringify(r).toLowerCase().includes(filtertext.toLowerCase())),
	);

	// Base-ordered by class so rows don't shuffle between polls (the page's stable tiebreak).
	const sorted = $derived([...actors].sort((a, b) => a.class_name.localeCompare(b.class_name)));

	let expanded = $state<Set<string>>(new Set());
	function toggle(id: string) {
		const s = new Set(expanded);
		if (s.has(id)) s.delete(id);
		else s.add(id);
		expanded = s;
	}

	// Mirrored from routes/actors/+page.svelte — the same states must wear the same colours here.
	function stateVariant(s: string): 'success' | 'secondary' | 'destructive' {
		if (s === 'ALIVE') return 'success';
		if (s === 'DEAD') return 'destructive';
		return 'secondary';
	}
	function tile(s: string): string {
		if (s === 'ALIVE') return 'bg-emerald-500/12 text-emerald-600 dark:text-emerald-400';
		if (s === 'DEAD') return 'bg-destructive/12 text-destructive';
		return 'bg-amber-500/12 text-amber-600 dark:text-amber-400';
	}
	function actorIcon(cls: string): typeof Boxes {
		if (cls.startsWith('ServeReplica')) return ServerCog;
		if (cls.includes('Proxy')) return Network;
		if (cls.includes('Controller')) return Gauge;
		if (cls.includes('Job')) return ListTree;
		if (cls.includes('Worker')) return Cpu;
		return Boxes;
	}
	function resourceChip(key: string): string {
		if (key === 'GPU') return 'bg-violet-500/15 text-violet-600 dark:text-violet-400';
		if (key === 'CPU') return 'bg-sky-500/15 text-sky-600 dark:text-sky-400';
		return 'bg-muted text-muted-foreground';
	}
	function nodeLabel(a: ActorInfo): string {
		return a.ip_address || a.node_id?.slice(0, 12) || '—';
	}
	function fmtUptime(secs: number | null): string {
		if (secs == null) return '—';
		secs = Math.max(0, secs);
		if (secs < 60) return `${Math.floor(secs)}s`;
		if (secs < 3600) return `${Math.floor(secs / 60)}m`;
		if (secs < 86400) return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
		return `${Math.floor(secs / 86400)}d ${Math.floor((secs % 86400) / 3600)}h`;
	}
	function actorAge(a: ActorInfo): number | null {
		if (!a.start_time_ms) return null;
		const end = a.state === 'DEAD' && a.end_time_ms ? a.end_time_ms : Date.now();
		return (end - a.start_time_ms) / 1000;
	}
	function fmtBytes(b: number | null): string {
		if (b == null) return '—';
		if (b < 1024 ** 2) return `${(b / 1024).toFixed(0)} KB`;
		if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(0)} MB`;
		return `${(b / 1024 ** 3).toFixed(1)} GB`;
	}

	function select(node: HTMLElement, actor: ActorInfo) {
		node.dispatchEvent(
			new CustomEvent(RASK_SELECT, {
				bubbles: true,
				composed: true,
				detail: {
					source: 'rask-compute-actors',
					kind: 'ray-actor',
					id: actor.actor_id ?? actor.class_name,
					label: actor.name ?? actor.class_name,
				} satisfies SelectDetail,
			}),
		);
	}
</script>

<div class="bg-background block h-full overflow-auto p-3">
	<p class="text-muted-foreground mb-2 text-[11px]">mounted {poll.mountedAt} · poll #{poll.polls}</p>
	{#if filtertext.trim() !== '' && shownactors.length !== actors.length}
		<p class="text-muted-foreground mb-1 text-[11px]">
			filtered: {shownactors.length}/{actors.length} match “{filtertext}”
		</p>
	{/if}
	{#if poll.error !== null}
		<p class="text-destructive text-sm">Ray unreachable: {poll.error}</p>
	{:else if actors.length === 0}
		<p class="text-muted-foreground text-sm">No actors on the cluster.</p>
	{:else}
		<Card class="overflow-hidden">
			<div class="max-h-full overflow-auto">
				<table class="w-full border-collapse text-xs">
					<thead class="bg-card sticky top-0 z-10 text-left">
						<tr class="border-b">
							<th class="px-3 py-2">state</th>
							<th class="px-3 py-2">namespace</th>
							<th class="px-3 py-2">node</th>
							<th class="px-3 py-2">uptime</th>
							<th class="px-3 py-2">cpu</th>
							<th class="px-3 py-2">tasks (run·pend)</th>
							<th class="px-3 py-2">gpu</th>
							<th class="px-3 py-2">actor</th>
							<th class="px-3 py-2"></th>
						</tr>
					</thead>
					<tbody>
						{#each sorted as a (a.actor_id ?? a.class_name)}
							{@const Icon = actorIcon(a.class_name)}
							{@const open = expanded.has(a.actor_id ?? '')}
							{@const run = a.num_running_tasks ?? 0}
							{@const pend = a.num_pending_tasks ?? 0}
							<tr
								class="border-border/40 hover:bg-muted/40 cursor-pointer border-b {a.state ===
								'DEAD'
									? 'opacity-70'
									: ''}"
								onclick={(e) => {
	select(e.currentTarget, a);
	toggle(a.actor_id ?? '');
}}
							>
								<td class="px-3 py-1.5"><Badge variant={stateVariant(a.state)}>{a.state}</Badge></td>
								<td class="px-3 py-1.5 font-mono">{a.ray_namespace ?? '—'}</td>
								<td class="px-3 py-1.5 font-mono">{nodeLabel(a)}</td>
								<td class="px-3 py-1.5 font-mono tabular-nums">{fmtUptime(actorAge(a))}</td>
								<td class="px-3 py-1.5 font-mono tabular-nums">
									{#if a.cpu_percent != null}
										<div class="flex items-center gap-1.5">
											<div class="bg-muted h-1.5 w-10 shrink-0 overflow-hidden rounded-full">
												<div class="h-full bg-sky-500" style:width={`${Math.min(100, a.cpu_percent)}%`}></div>
											</div>
											<span>{a.cpu_percent.toFixed(0)}%</span>
										</div>
									{:else}<span class="text-muted-foreground">—</span>{/if}
								</td>
								<td class="px-3 py-1.5 font-mono tabular-nums">
									{#if run || pend}
										<span class={run ? 'text-emerald-600 dark:text-emerald-400' : ''}>{run}</span>
										<span class="text-muted-foreground">·</span>
										<span class={pend ? 'text-amber-600 dark:text-amber-400' : ''}>{pend}</span>
									{:else}<span class="text-muted-foreground">—</span>{/if}
								</td>
								<td class="px-3 py-1.5 font-mono tabular-nums">
									{#if a.gpu_mem_mb != null || a.gpu_util != null}
										{a.gpu_util != null ? `${a.gpu_util.toFixed(0)}%` : ''}
										{#if a.gpu_mem_mb}<span class="text-violet-600 dark:text-violet-400"
												>· {a.gpu_mem_mb} MB</span
											>{/if}
									{:else}<span class="text-muted-foreground">—</span>{/if}
								</td>
								<td class="px-3 py-1.5">
									<div class="flex items-center gap-2">
										<div
											class="flex h-6 w-6 shrink-0 items-center justify-center rounded-md {tile(
												a.state,
											)}"
										>
											<Icon class="h-3.5 w-3.5" />
										</div>
										<div>
											<div class="font-mono" title={a.actor_id ?? ''}>{a.class_name}</div>
											{#if a.death_reason}
												<div
													class="text-destructive flex items-start gap-0.5 font-mono text-[10px]"
													title={a.death_reason}
												>
													<TriangleAlert class="mt-0.5 h-3 w-3 shrink-0" />{a.death_reason}
												</div>
											{:else if a.repr_name}
												<div class="text-muted-foreground font-mono text-[10px]">
													{a.repr_name}
												</div>
											{/if}
										</div>
									</div>
								</td>
								<td class="px-3 py-1.5">
									<ChevronRight
										class="text-muted-foreground h-3.5 w-3.5 transition-transform {open
											? 'rotate-90'
											: ''}"
									/>
								</td>
							</tr>
							{#if open}
								<tr class="bg-muted/20 border-border/40 border-b">
									<td colspan="9" class="px-4 py-3">
										<div
											class="grid grid-cols-2 gap-x-6 gap-y-1 font-mono text-[11px] sm:grid-cols-3 lg:grid-cols-4"
										>
											{#each [{ k: 'actor_id', v: a.actor_id }, { k: 'pid', v: a.pid }, { k: 'worker_id', v: a.worker_id }, { k: 'ip', v: a.ip_address }, { k: 'job_id', v: a.job_id }, { k: 'restarts', v: a.num_restarts }, { k: 'detached', v: String(a.is_detached) }, { k: 'placement_group', v: a.placement_group_id ?? '—' }, { k: 'rss mem', v: fmtBytes(a.rss_bytes) }, { k: 'open fds', v: a.num_fds ?? '—' }, { k: 'tasks executed', v: a.num_executed_tasks ?? '—' }, { k: 'task queue', v: a.task_queue_length ?? '—' }] as row (row.k)}
												<div class="flex justify-between gap-2 border-b border-dashed py-0.5">
													<span class="text-muted-foreground">{row.k}</span>
													<span class="truncate" title={String(row.v ?? '')}>{row.v ?? '—'}</span>
												</div>
											{/each}
										</div>
										{#if Object.keys(a.required_resources ?? {}).length}
											<div class="mt-2 flex flex-wrap items-center gap-1">
												<span class="text-muted-foreground text-[10px] uppercase">reserves</span>
												{#each Object.entries(a.required_resources).filter(([, v]) => v) as [k, v] (k)}
													<span
														class="rounded-md px-1.5 py-0.5 text-[10px] font-medium {resourceChip(
															k,
														)}">{v}× {k}</span
													>
												{/each}
											</div>
										{/if}
									</td>
								</tr>
							{/if}
						{/each}
					</tbody>
				</table>
			</div>
		</Card>
	{/if}
</div>
