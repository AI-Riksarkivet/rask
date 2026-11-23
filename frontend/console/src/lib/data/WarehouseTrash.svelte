<script lang="ts">
	/**
	 * A warehouse's trash: each recoverable drop under it that the caller holds a grant on, as the
	 * catalog's trash listing answers it, with an Undrop for each one short of its deadline. Restoring is
	 * the only act offered: ending a record or releasing its bytes is maintenance's, past the deadline.
	 */
	import { Undo2 } from '@lucide/svelte';
	import { Badge } from '@riksarkivet/ui/badge';
	import { Button } from '@riksarkivet/ui/button';
	import * as Table from '@riksarkivet/ui/table';
	import { formatZoned } from '@riksarkivet/ui/utils';
	import { catalogPath } from '#lib/catalog-trail.ts';
	import { undropTable, type TrashEntry } from './remote/catalog.api.ts';
	import { undropNamespace } from './remote/namespace.api.ts';
	import { fetchWarehouseTrash } from './remote/warehouses.api.ts';

	let { warehouse, onrestored }: { warehouse: string; onrestored: () => void } = $props();

	const trashRead = $derived(fetchWarehouseTrash(warehouse));
	const answer = $derived(trashRead.current);

	/** What tells one row from every other, across warehouses. An entry's uuid does, and a record that
	 *  names none (the catalog answers `uuid: ""` for it) is told apart by its place in the listing. */
	const rowKey = (entry: TrashEntry, at: number): string =>
		`${warehouse} ${entry.uuid || `${entry.kind} ${entry.id} ${at}`}`;

	/** The row whose undrop is out; one at a time. */
	let restoring = $state<string | null>(null);
	/** The catalog's `detail` for each row whose undrop it refused. */
	let refusals = $state<Record<string, string>>({});

	async function undrop(entry: TrashEntry, row: string): Promise<void> {
		if (restoring !== null) return;
		restoring = row;
		const { [row]: _previous, ...others } = refusals;
		refusals = others;
		// The read of the warehouse the undrop came from: the page may show another by the time it is answered.
		const listing = trashRead;
		const uuid = entry.uuid === '' ? undefined : entry.uuid;
		try {
			const res =
				entry.kind === 'table'
					? await undropTable({ table: entry.id, uuid })
					: await undropNamespace({ namespace: entry.id, uuid });
			if (res.ok) {
				await listing.refresh();
				onrestored();
			} else {
				refusals = { ...refusals, [row]: res.detail };
			}
		} finally {
			restoring = null;
		}
	}
</script>

<section class="mb-6" aria-labelledby="trash-heading">
	<h2 id="trash-heading" class="mb-2 text-sm font-bold">Trash</h2>
	{#if answer === undefined}
		<p class="text-muted-foreground text-xs">Loading…</p>
	{:else if !answer.ok && answer.status === 403}
		<p class="text-muted-foreground text-xs" data-testid="trash-refused">
			You may not see this warehouse’s trash.
		</p>
	{:else if !answer.ok}
		<p class="text-muted-foreground text-xs" data-testid="trash-failed">{answer.detail}</p>
	{:else if answer.data.objects.length === 0}
		<p class="text-muted-foreground text-xs">
			Nothing dropped from this warehouse is in its trash.
		</p>
	{:else}
		<Table.Root class="text-xs" aria-label="Dropped from this warehouse">
			<Table.Header>
				<Table.Row>
					<Table.Head>kind</Table.Head>
					<Table.Head>name</Table.Head>
					<Table.Head>dropped</Table.Head>
					<Table.Head>restorable until</Table.Head>
					<Table.Head><span class="sr-only">action</span></Table.Head>
				</Table.Row>
			</Table.Header>
			<Table.Body>
				{#each answer.data.objects as entry, at (rowKey(entry, at))}
					{@const path = catalogPath(entry.id)}
					{@const row = rowKey(entry, at)}
					<Table.Row>
						<Table.Cell><Badge variant="outline">{entry.kind}</Badge></Table.Cell>
						<Table.Cell class="font-mono">{path}</Table.Cell>
						<Table.Cell class="tabular-nums">{formatZoned(entry.dropped_at)}</Table.Cell>
						<Table.Cell class="tabular-nums">{formatZoned(entry.expires_at)}</Table.Cell>
						<Table.Cell class="text-right">
							{#if entry.expired}
								<span class="text-muted-foreground">Past its deadline</span>
							{:else}
								<Button
									size="sm"
									variant="outline"
									disabled={restoring !== null}
									aria-label={`Undrop ${path}, dropped ${formatZoned(entry.dropped_at)}`}
									onclick={() => undrop(entry, row)}
								>
									<Undo2 />
									{restoring === row ? 'Restoring…' : 'Undrop'}
								</Button>
							{/if}
							{#if refusals[row]}
								<p class="text-destructive mt-1 text-xs whitespace-normal" role="alert">
									{refusals[row]}
								</p>
							{/if}
						</Table.Cell>
					</Table.Row>
				{/each}
			</Table.Body>
		</Table.Root>
	{/if}
</section>
