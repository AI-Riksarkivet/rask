<!-- The app build (and svelte-check) compiles this WITHOUT customElement: true — correct, the app
     never mounts the wrapper; only vite.elements.config.ts compiles it as an element. -->
<!-- svelte-ignore options_missing_custom_element -->
<svelte:options customElement={{ shadow: 'none' }} />

<script lang="ts">
	/**
	 * `<rask-lakehouse-datasets>` — the governed datasets, VISUALLY IDENTICAL to the zone's
	 * /lineage/datasets list: the same Card-bounded table, the same namespace pill and tag chips
	 * (@rask/ui Badge), the same utility classes — compiled into this bundle by elements.css, since
	 * the host page's Tailwind build cannot generate them. Reads through the shared client; the mount
	 * stamp + poll counter stay as the no-remount witness; rows dispatch the rask:select contract
	 * event instead of navigating (a panel has no router).
	 */
	import { Badge } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import type { Datasets } from '@rask/api/lineage';
	import { RASK_SELECT, type SelectDetail } from '@rask/dockview/contract';
	import { ElementPoll } from './poll.svelte';
	import { client } from './store';

	let { pollms = 30000 }: { pollms?: number } = $props();
	const poll = new ElementPoll<Datasets>(async () => {
		const res = await client.listDatasets();
		if (!res.ok)
			throw new Error(
				`datasets: ${res.status === 401 ? 'session expired or no access' : `HTTP ${res.status}`}`,
			);
		return res.data;
	});
	$effect(() => poll.start(pollms));

	const datasets = $derived(poll.data?.datasets ?? []);

	// Mirrored from routes/lineage/datasets/+page.svelte — the same cells must read the same here.
	function tagsOf(tags: string[] | null | undefined): string[] {
		return tags ?? [];
	}

	function select(node: HTMLElement, name: string) {
		node.dispatchEvent(
			new CustomEvent(RASK_SELECT, {
				bubbles: true,
				composed: true,
				detail: {
					source: 'rask-lakehouse-datasets',
					kind: 'dataset',
					id: name,
					label: name,
				} satisfies SelectDetail,
			}),
		);
	}
</script>

<div class="bg-background block h-full overflow-auto p-3">
	<p class="text-muted-foreground mb-2 text-[11px]">mounted {poll.mountedAt} · poll #{poll.polls}</p>
	{#if poll.error !== null}
		<p class="text-destructive text-sm">Catalog unavailable: {poll.error}</p>
	{:else if datasets.length === 0}
		<p class="text-muted-foreground text-sm">
			No datasets yet — they appear as pipelines emit OpenLineage events.
		</p>
	{:else}
		<Card class="overflow-hidden">
			<div class="max-h-full overflow-auto">
				<table class="w-full border-collapse text-xs">
					<thead class="bg-muted/50 sticky top-0 z-10 text-left">
						<tr class="border-b">
							<th class="text-muted-foreground px-3 py-2 font-medium">dataset</th>
							<th class="text-muted-foreground w-44 px-3 py-2 font-medium">namespace</th>
							<th class="text-muted-foreground px-3 py-2 font-medium">tags</th>
						</tr>
					</thead>
					<tbody>
						{#each datasets as ds (ds.name)}
							<tr
								class="hover:bg-muted/50 cursor-pointer border-b transition-colors"
								onclick={(e) => select(e.currentTarget, ds.name)}
							>
								<td class="px-3 py-2 align-middle font-mono whitespace-nowrap">{ds.name}</td>
								<td class="px-3 py-2 align-middle whitespace-nowrap">
									{#if ds.namespace}
										<Badge variant="secondary" class="bg-accent/20 text-foreground px-2 py-px text-[11px]"
											>{ds.namespace}</Badge
										>
									{:else}<span class="text-muted-foreground">—</span>{/if}
								</td>
								<td class="px-3 py-2 align-middle whitespace-nowrap">
									{#if tagsOf(ds.tags).length}
										<span class="inline-flex flex-wrap items-center gap-1">
											{#each tagsOf(ds.tags) as t (t)}
												<Badge variant="outline" class="text-muted-foreground px-1.5 py-px text-[10px]"
													>{t}</Badge
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
