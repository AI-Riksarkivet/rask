<!-- The app build (and svelte-check) compiles this WITHOUT customElement: true — correct, the app
     never mounts the wrapper; only vite.elements.config.ts compiles it as an element. -->
<!-- svelte-ignore options_missing_custom_element -->
<svelte:options customElement={{ shadow: 'none' }} />

<script lang="ts">
	/**
	 * `<rask-lakehouse-tables>` — the governed table registry, served by the lakehouse zone to the
	 * global workbench. Mirrors `/lakehouse/catalog/tables` (TableRegistry): the `<ns>$<table>` list
	 * with its namespace column and medallion stage badge, in the same Card-bounded table dress.
	 *
	 * Transport: `GET /lakehouse/capi/v1/table` through the zone's GET-only catalog proxy (the page
	 * reaches the same upstream through a remote function, which an element cannot import). The
	 * stage helpers come from the zone's own `$lib/data/stage` — plain functions, no `$app` imports,
	 * so the element and the page classify identically.
	 */
	import { Badge } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import { RASK_SELECT, type SelectDetail } from '@rask/dockview/contract';
	import { namespaceOfTable, stageOfTable } from '../data/stage';
	import { ElementPoll } from './poll.svelte';

	let { pollms = 30000, filtertext = '' }: { pollms?: number; filtertext?: string } = $props();

	const poll = new ElementPoll<string[]>(async (f) => {
		const res = await f('/lakehouse/capi/v1/table');
		if (res.status === 401) throw new Error('session expired or no access');
		if (!res.ok) throw new Error(`HTTP ${res.status}`);
		const body = (await res.json()) as { tables?: string[] };
		return [...(body.tables ?? [])].sort();
	});
	$effect(() => poll.start(pollms));

	const tables = $derived(poll.data ?? []);
	const showntables = $derived(
		filtertext.trim() === ''
			? tables
			: tables.filter((t) => t.toLowerCase().includes(filtertext.toLowerCase())),
	);

	function select(node: HTMLElement, table: string) {
		node.dispatchEvent(
			new CustomEvent(RASK_SELECT, {
				bubbles: true,
				composed: true,
				detail: {
					source: 'rask-lakehouse-tables',
					kind: 'catalog-table',
					id: table,
					label: table,
				} satisfies SelectDetail,
			}),
		);
	}
</script>

<div class="bg-background block h-full overflow-auto p-3">
	<p class="text-muted-foreground mb-2 text-[11px]">mounted {poll.mountedAt} · poll #{poll.polls}</p>
	{#if filtertext.trim() !== '' && showntables.length !== tables.length}
		<p class="text-muted-foreground mb-1 text-[11px]">
			filtered: {showntables.length}/{tables.length} match “{filtertext}”
		</p>
	{/if}
	{#if poll.error !== null}
		<p class="text-destructive text-sm">Catalog unavailable: {poll.error}</p>
	{:else if tables.length === 0}
		<p class="text-muted-foreground text-sm">No tables declared yet.</p>
	{:else}
		<Card class="overflow-hidden">
			<div class="max-h-full overflow-auto">
				<table class="w-full border-collapse text-xs">
					<thead class="bg-muted/50 sticky top-0 text-left">
						<tr class="border-b">
							<th class="text-muted-foreground px-3 py-2 font-medium">table</th>
							<th class="text-muted-foreground w-44 px-3 py-2 font-medium">namespace</th>
							<th class="text-muted-foreground px-3 py-2 font-medium">stage</th>
						</tr>
					</thead>
					<tbody>
						{#each showntables as t (t)}
							{@const stage = stageOfTable(t)}
							<tr
								class="hover:bg-muted/50 cursor-pointer border-b transition-colors"
								onclick={(e) => select(e.currentTarget, t)}
							>
								<td class="px-3 py-2 align-middle font-mono whitespace-nowrap">{t}</td>
								<td class="px-3 py-2 align-middle whitespace-nowrap">
									<Badge variant="secondary">{namespaceOfTable(t)}</Badge>
								</td>
								<td class="px-3 py-2 align-middle whitespace-nowrap">
									{#if stage}
										<Badge variant="outline">{stage.stage}</Badge>
									{:else}
										<span class="text-muted-foreground">—</span>
									{/if}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		</Card>
	{/if}
</div>
