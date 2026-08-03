<!-- The app build (and svelte-check) compiles this WITHOUT customElement: true — correct, the app
     never mounts the wrapper; only vite.elements.config.ts compiles it as an element. -->
<!-- svelte-ignore options_missing_custom_element -->
<svelte:options customElement={{ shadow: 'none' }} />

<script lang="ts">
	/**
	 * `<rask-compute-jobs>` — the Ray jobs panel, VISUALLY IDENTICAL to the zone's /compute/jobs
	 * table: same Card, same Badge variants, same utility classes (compiled into this bundle by
	 * elements.css — the host cannot generate them). The mount stamp + poll counter stay as the
	 * no-remount witness; rows dispatch the rask:select contract event.
	 */
	import { Badge, type BadgeVariant } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import { rayJobs, type RayJob, type RayJobsPayload } from '@rask/api';
	import { RASK_SELECT, type SelectDetail } from '@rask/dockview/contract';
	import { RayPoll } from './ray-poll.svelte';

	let { pollms = 5000, filtertext = '' }: { pollms?: number; filtertext?: string } = $props();
	const poll = new RayPoll<RayJobsPayload>((f) => rayJobs(f));
	$effect(() => poll.start(pollms));

	const jobs = $derived(poll.data?.jobs ?? []);
	/** The cross-filter (wave 3): the compositor pushes the active selection's label down as a
	 *  property; rows narrow by substring over their serialized form — generic on purpose, every
	 *  list element filters the same way. */
	const shownjobs = $derived(
		filtertext.trim() === ''
			? jobs
			: jobs.filter((r) => JSON.stringify(r).toLowerCase().includes(filtertext.toLowerCase())),
	);

	// Mirrored from routes/jobs/+page.svelte — the same states must wear the same colours here.
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

	function select(node: HTMLElement, job: RayJob) {
		node.dispatchEvent(
			new CustomEvent(RASK_SELECT, {
				bubbles: true,
				composed: true,
				detail: {
					source: 'rask-compute-jobs',
					kind: 'ray-job',
					id: job.submission_id,
					label: job.entrypoint ?? job.submission_id,
				} satisfies SelectDetail,
			}),
		);
	}
</script>

<div class="bg-background block h-full overflow-auto p-3">
	<p class="text-muted-foreground mb-2 text-[11px]">mounted {poll.mountedAt} · poll #{poll.polls}</p>
	{#if filtertext.trim() !== '' && shownjobs.length !== jobs.length}
		<p class="text-muted-foreground mb-1 text-[11px]">
			filtered: {shownjobs.length}/{jobs.length} match “{filtertext}”
		</p>
	{/if}
	{#if poll.error !== null}
		<p class="text-destructive text-sm">Ray unreachable: {poll.error}</p>
	{:else if jobs.length === 0}
		<p class="text-muted-foreground text-sm">No job submissions.</p>
	{:else}
		<Card class="overflow-hidden">
			<div class="max-h-full overflow-auto">
				<table class="w-full border-collapse text-xs">
					<thead class="bg-card sticky top-0 z-10 text-left">
						<tr class="border-b">
							<th class="px-3 py-2">status</th>
							<th class="px-3 py-2">submission_id</th>
							<th class="px-3 py-2">started</th>
							<th class="px-3 py-2">runtime</th>
							<th class="px-3 py-2">entrypoint</th>
						</tr>
					</thead>
					<tbody>
						{#each shownjobs as j (j.submission_id)}
							<tr
								class="border-border/40 hover:bg-muted/40 cursor-pointer border-b"
								onclick={(e) => select(e.currentTarget, j)}
							>
								<td class="px-3 py-1.5">
									<Badge variant={variantFor(j.status)} class={j.status === 'RUNNING' ? 'animate-pulse' : ''}
										>{j.status}</Badge
									>
								</td>
								<td class="px-3 py-1.5 font-mono">{j.submission_id}</td>
								<td class="text-muted-foreground px-3 py-1.5 font-mono">{fmtTime(j.start_time)}</td>
								<td class="px-3 py-1.5 font-mono tabular-nums">{fmtRuntime(j.start_time, j.end_time)}</td>
								<td class="text-muted-foreground px-3 py-1.5 font-mono">{j.entrypoint ?? '—'}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		</Card>
	{/if}
</div>
