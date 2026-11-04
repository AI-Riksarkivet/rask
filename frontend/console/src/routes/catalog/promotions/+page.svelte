<script lang="ts">
	import { onMount } from 'svelte';
	import { Badge } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import { reasonLabel, type PromotionHold } from '$lib/data/promotions';
	import { listPromotions } from '$lib/data/remote/promotions.api';
	import { formatAbsolute } from '@rask/ui/utils';

	// The open holds the caller may see, in table order, one page at a time. A hold names its table as
	// the table is named now, so each row links to the table's own hold.
	let holds = $state<PromotionHold[]>([]);
	let nextPage = $state<string | null>(null);
	let status = $state(0);
	let detail = $state('');
	let settled = $state(false);

	async function load(pageToken?: string): Promise<void> {
		const res = await listPromotions(pageToken ? { pageToken } : {});
		settled = true;
		if (!res.ok) {
			status = res.status;
			detail = res.detail;
			return;
		}
		status = 200;
		holds = pageToken ? [...holds, ...res.data.holds] : res.data.holds;
		nextPage = res.data.page_token ?? null;
	}

	onMount(() => {
		void load();
	});
</script>

<svelte:head><title>Promotions · rask</title></svelte:head>

<div class="flex flex-col gap-4 p-4">
	<div>
		<h1 class="text-lg font-semibold">Promotions</h1>
		<p class="text-muted-foreground text-sm">
			A version a project's quality gate found unusual rather than broken waits here for a validator
			instead of being published. Approving moves <code>published</code> to it; rejecting restores
			the table to what <code>published</code> names.
		</p>
	</div>

	{#if !settled}
		<Card class="p-4"><p class="text-muted-foreground text-sm">Loading…</p></Card>
	{:else if status === 401}
		<Card class="p-4"><p class="text-sm">Sign in to see held promotions.</p></Card>
	{:else if status !== 200}
		<Card class="p-4">
			<p class="text-sm">Could not read the held promotions ({status || 'offline'}). {detail}</p>
		</Card>
	{:else if holds.length === 0}
		<Card class="p-4">
			<p class="text-muted-foreground text-sm">No promotion is waiting for a decision.</p>
		</Card>
	{:else}
		<Card class="p-0">
			<table class="w-full text-sm" data-testid="promotions">
				<thead class="text-muted-foreground text-left text-xs">
					<tr>
						<th class="p-2">Table</th>
						<th class="p-2">Held</th>
						<th class="p-2">Published</th>
						<th class="p-2">Why</th>
						<th class="p-2">Expires</th>
					</tr>
				</thead>
				<tbody>
					{#each holds as hold (hold.uuid)}
						<tr class="border-t">
							<td class="p-2">
								<a
									class="mono underline"
									href={`/catalog/promotions/${encodeURIComponent(hold.table)}`}>{hold.table}</a
								>
							</td>
							<td class="p-2"><Badge variant="warning">v{hold.version}</Badge></td>
							<td class="p-2"
								>{hold.published_version === null ? '—' : `v${hold.published_version}`}</td
							>
							<td class="p-2">{hold.reasons.map(reasonLabel).join('; ')}</td>
							<td class="p-2">{formatAbsolute(hold.expires_at)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</Card>
		{#if nextPage}
			<button class="text-sm underline" onclick={() => load(nextPage ?? undefined)}
				>Load more</button
			>
		{/if}
	{/if}
</div>
