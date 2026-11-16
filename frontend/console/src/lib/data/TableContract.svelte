<script lang="ts">
	/**
	 * The Contract block of a table's Overview (ADR 0212): the ODCS data contract the table holds, read
	 * from the catalog's `contract/describe`, shown and never written here. Its owner sets one at the
	 * catalog's `contract/set` door, and the catalog holds every commit to the table to it. A refused
	 * or failed read is said in one line, so the rest of the page stands.
	 */
	import { Badge } from '@rask/ui/badge';
	import * as Table from '@rask/ui/table';
	import { formatZoned } from '@rask/ui/utils';
	import { Subject } from '#lib/access/identity/index.ts';
	import { contractColumns } from './contract.ts';
	import { fetchTableContract } from './remote/catalog.api.ts';

	let { table }: { table: string } = $props();

	const contractRead = $derived(fetchTableContract(table));
	const answer = $derived(contractRead.current);
	const document = $derived(answer?.ok ? (answer.data.contract ?? null) : null);
	const columns = $derived(document ? contractColumns(document) : []);
</script>

<section class="mb-5" aria-labelledby="contract-heading" data-testid="table-contract">
	<h2
		id="contract-heading"
		class="text-muted-foreground mb-2 text-[13px] font-semibold tracking-wider uppercase"
	>
		Contract
	</h2>
	{#if answer === undefined}
		<p class="text-muted-foreground text-xs">Loading…</p>
	{:else if !answer.ok}
		<p class="text-muted-foreground text-xs">
			The contract could not be read ({answer.status === 0
				? 'the catalog did not answer'
				: `HTTP ${answer.status}`}): {answer.detail}
		</p>
	{:else if document === null}
		<p class="text-muted-foreground text-xs">
			This table holds no contract. Its owner sets one at the catalog's <code>contract/set</code> door.
		</p>
	{:else}
		<dl class="mb-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
			<div class="flex items-center gap-1.5">
				<dt class="text-muted-foreground">id</dt>
				<dd class="font-mono">{document.id}</dd>
			</div>
			<div class="flex items-center gap-1.5">
				<dt class="text-muted-foreground">version</dt>
				<dd class="font-mono">{document.version}</dd>
			</div>
			<div class="flex items-center gap-1.5">
				<dt class="text-muted-foreground">status</dt>
				<dd><Badge variant="outline" class="font-normal">{document.status}</Badge></dd>
			</div>
			<div class="flex items-center gap-1.5">
				<dt class="text-muted-foreground">set by</dt>
				<dd><Subject value={answer.data.set_by} fallback="unknown" /></dd>
			</div>
			{#if answer.data.set_at}
				<div class="flex items-center gap-1.5">
					<dt class="text-muted-foreground">set at</dt>
					<dd class="tabular-nums">{formatZoned(answer.data.set_at)}</dd>
				</div>
			{/if}
		</dl>
		{#if columns.length === 0}
			<p class="text-muted-foreground text-xs">The contract names no column.</p>
		{:else}
			<Table.Root class="text-xs" aria-label="Columns the contract names">
				<Table.Header>
					<Table.Row>
						<Table.Head>column</Table.Head>
						<Table.Head>physical type</Table.Head>
						<Table.Head>required</Table.Head>
						<Table.Head>primary key</Table.Head>
					</Table.Row>
				</Table.Header>
				<Table.Body>
					{#each columns as column (column.path)}
						<Table.Row>
							<Table.Cell class="font-mono">{column.path}</Table.Cell>
							<Table.Cell class="font-mono">{column.physicalType ?? '—'}</Table.Cell>
							<Table.Cell>{column.required ? 'yes' : 'no'}</Table.Cell>
							<Table.Cell>{column.primaryKey ? 'yes' : 'no'}</Table.Cell>
						</Table.Row>
					{/each}
				</Table.Body>
			</Table.Root>
		{/if}
	{/if}
</section>
