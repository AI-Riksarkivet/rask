<!-- The app build (and svelte-check) compiles this WITHOUT customElement: true — correct, the app
     never mounts the wrapper; only vite.elements.config.ts compiles it as an element. -->
<!-- svelte-ignore options_missing_custom_element -->
<svelte:options customElement={{ shadow: 'none' }} />

<script lang="ts">
	/**
	 * `<rask-lakehouse-jobs>` — the governed compute identities, VISUALLY IDENTICAL to the zone's
	 * /lineage/jobs list: the same Card-bounded table, the same job/namespace/writes columns, the
	 * same namespace pill and output chips (@rask/ui Badge), the same utility classes — compiled
	 * into this bundle by elements.css, since the host page's Tailwind build cannot generate them.
	 * Reads through the shared client (`GET /lakehouse/api/jobs`, root-absolute, so it answers from
	 * any zone's page); the mount stamp + poll counter stay as the no-remount witness; rows dispatch
	 * the rask:select contract event instead of navigating to the detail route (a panel has no
	 * router). Page chrome the panel drops on purpose: SortHeader sorting, the DataTableTextFilter
	 * (the compositor's cross-filter replaces it) and pagination — all of them need routing or
	 * table state a 200px-wide dock panel has no room for.
	 */
	import { Badge } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import type { Jobs, JobSummary } from '@rask/api/lineage';
	import { RASK_SELECT, type SelectDetail } from '@rask/dockview/contract';
	import { ElementPoll } from './poll.svelte';
	import { client } from './store';

	let { pollms = 30000, filtertext = '' }: { pollms?: number; filtertext?: string } = $props();
	const poll = new ElementPoll<Jobs>(async () => {
		const res = await client.listJobs();
		if (!res.ok)
			throw new Error(
				`jobs: ${res.status === 401 ? 'session expired or no access' : `HTTP ${res.status}`}`,
			);
		return res.data;
	});
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

	// Mirrored from routes/lineage/jobs/+page.svelte — the same cells must read the same here.
	function outputsOf(outputs: string[] | null | undefined): string[] {
		return outputs ?? [];
	}
	/** Job identity is `namespace/name` (the detail route's rest segments); a namespace-less job is
	 *  addressed by bare name. Mirrored from the page's jobHref, minus the routing. */
	function jobId(j: JobSummary): string {
		return j.namespace ? `${j.namespace}/${j.name}` : j.name;
	}

	function select(node: HTMLElement, job: JobSummary) {
		node.dispatchEvent(
			new CustomEvent(RASK_SELECT, {
				bubbles: true,
				composed: true,
				detail: {
					source: 'rask-lakehouse-jobs',
					kind: 'lineage-job',
					id: jobId(job),
					label: job.name,
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
		<p class="text-destructive text-sm">Lineage unavailable: {poll.error}</p>
	{:else if jobs.length === 0}
		<p class="text-muted-foreground text-sm">
			No jobs recorded yet — they appear as pipelines emit OpenLineage events.
		</p>
	{:else}
		<Card class="overflow-hidden">
			<div class="max-h-full overflow-auto">
				<table class="w-full border-collapse text-xs">
					<thead class="bg-muted/50 sticky top-0 z-10 text-left">
						<tr class="border-b">
							<th class="text-muted-foreground px-3 py-2 font-medium">job</th>
							<th class="text-muted-foreground w-44 px-3 py-2 font-medium">namespace</th>
							<th class="text-muted-foreground px-3 py-2 font-medium">writes</th>
						</tr>
					</thead>
					<tbody>
						{#each shownjobs as job (jobId(job))}
							<tr
								class="hover:bg-muted/50 cursor-pointer border-b transition-colors"
								onclick={(e) => select(e.currentTarget, job)}
							>
								<td class="px-3 py-2 align-middle font-mono whitespace-nowrap">{job.name}</td>
								<td class="px-3 py-2 align-middle whitespace-nowrap">
									{#if job.namespace}
										<Badge variant="secondary" class="bg-accent/20 text-foreground px-2 py-px text-[11px]"
											>{job.namespace}</Badge
										>
									{:else}<span class="text-muted-foreground">—</span>{/if}
								</td>
								<td class="px-3 py-2 align-middle whitespace-nowrap">
									{#if outputsOf(job.outputs).length}
										<span class="inline-flex flex-wrap items-center gap-1">
											{#each outputsOf(job.outputs) as out (out)}
												<Badge
													variant="outline"
													class="text-muted-foreground px-1.5 py-px font-mono text-[10px]">{out}</Badge
												>
											{/each}
										</span>
									{:else}<span class="text-muted-foreground">—</span>{/if}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		</Card>
	{/if}
</div>
