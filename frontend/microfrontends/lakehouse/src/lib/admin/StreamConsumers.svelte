<script lang="ts">
	// One stream card's consumer rows on the shared @rask/ui DataTable (goal cond 4): sortable
	// columns, the same diagnostic hooks as before — pending/ack-pending pressure, redelivered as
	// the wedge signal (warn tone), and the >10-min stale chip judged against the MONITOR's clock
	// (`now`), never the browser's.
	import {
		createSvelteTable,
		DataTable,
		DataTableHeaderButton,
		getCoreRowModel,
		getPaginationRowModel,
		getSortedRowModel,
		renderComponent,
		renderSnippet,
		type ColumnDef,
		type PaginationState,
		type SortingState,
	} from '@rask/ui/data-table';
	import * as Sheet from '@rask/ui/sheet';
	import { ExternalLink } from '@lucide/svelte';
	import { base } from '$app/paths';
	import type { JetStreamConsumer } from './jetstream';

	let { consumers, now, stream }: { consumers: JetStreamConsumer[]; now: string; stream: string } =
		$props();

	// A consumer whose last delivery activity is >10 min behind the monitor's own clock is stale:
	// on an active fabric that usually means its app stopped reading (wedged subscriber).
	const STALE_MS = 10 * 60 * 1000;
	function isStale(lastActive: string | undefined): boolean {
		if (!lastActive) return false;
		const active = new Date(lastActive).getTime();
		const ref = new Date(now).getTime();
		return !Number.isNaN(active) && !Number.isNaN(ref) && ref - active > STALE_MS;
	}
	function when(ts: string | undefined): string {
		if (!ts) return '—';
		const d = new Date(ts);
		return Number.isNaN(d.getTime()) ? ts : d.toLocaleString();
	}

	// ── the row drawer (goal cond 8): click a consumer → the full record + the lag diagnosis. ──
	let drawerConsumer = $state<JetStreamConsumer | null>(null);

	let sorting = $state<SortingState>([]);
	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: 10 });

	const sortableHeader =
		(label: string) =>
		({
			column,
		}: {
			column: {
				getIsSorted(): false | 'asc' | 'desc';
				getToggleSortingHandler(): ((e: Event) => void) | undefined;
			};
		}) =>
			renderComponent(DataTableHeaderButton, {
				label,
				sorted: column.getIsSorted(),
				onclick: column.getToggleSortingHandler(),
			});

	const columns: ColumnDef<JetStreamConsumer>[] = [
		{
			id: 'service',
			accessorKey: 'service',
			header: sortableHeader('service'),
			cell: ({ row }) => renderSnippet(serviceCell, row.original),
		},
		{
			id: 'consumer',
			accessorKey: 'name',
			header: sortableHeader('consumer'),
			cell: ({ row }) => renderSnippet(consumerCell, row.original),
		},
		{
			id: 'pending',
			accessorKey: 'num_pending',
			header: sortableHeader('pending'),
			cell: ({ row }) => renderSnippet(numCell, { n: row.original.num_pending, warn: false }),
			meta: { headerClass: 'w-24 text-right', cellClass: 'text-right' },
		},
		{
			id: 'ack-pending',
			accessorKey: 'num_ack_pending',
			header: sortableHeader('ack-pending'),
			cell: ({ row }) => renderSnippet(numCell, { n: row.original.num_ack_pending, warn: false }),
			meta: { headerClass: 'w-28 text-right', cellClass: 'text-right' },
		},
		{
			id: 'redelivered',
			accessorKey: 'num_redelivered',
			header: sortableHeader('redelivered'),
			cell: ({ row }) => renderSnippet(numCell, { n: row.original.num_redelivered, warn: true }),
			meta: { headerClass: 'w-28 text-right', cellClass: 'text-right' },
		},
		{
			id: 'last-active',
			accessorFn: (c) => c.last_active ?? '',
			header: sortableHeader('last active'),
			cell: ({ row }) => renderSnippet(activeCell, row.original),
			meta: { cellClass: 'whitespace-nowrap' },
		},
	];

	const table = createSvelteTable({
		get data() {
			return consumers;
		},
		columns,
		state: {
			get sorting() {
				return sorting;
			},
			get pagination() {
				return pagination;
			},
		},
		onSortingChange: (u) => (sorting = typeof u === 'function' ? u(sorting) : u),
		onPaginationChange: (u) => (pagination = typeof u === 'function' ? u(pagination) : u),
		getCoreRowModel: getCoreRowModel(),
		getSortedRowModel: getSortedRowModel(),
		getPaginationRowModel: getPaginationRowModel(),
	});
</script>

{#snippet serviceCell(c: JetStreamConsumer)}
	<span class="service mono" class:stale={isStale(c.last_active)}>{c.service}</span>
{/snippet}
{#snippet consumerCell(c: JetStreamConsumer)}
	<span class="mono" class:stale={isStale(c.last_active)}>
		{c.durable ? c.name : `${c.name} (ephemeral)`}
	</span>
{/snippet}
{#snippet numCell({ n, warn }: { n: number; warn: boolean })}
	<span class="num mono" class:pend={!warn && n > 0} class:warn={warn && n > 0}>{n}</span>
{/snippet}
{#snippet activeCell(c: JetStreamConsumer)}
	<span class="faint mono" class:stale={isStale(c.last_active)}>
		{when(c.last_active)}
		{#if isStale(c.last_active)}
			<span class="stalechip" title="No delivery activity for over 10 minutes">stale</span>
		{/if}
	</span>
{/snippet}

<DataTable {table} onrowclick={(c) => (drawerConsumer = c)} emptyMessage="No consumers bound." />

<Sheet.Root
	open={drawerConsumer !== null}
	onOpenChange={(o) => {
	if (!o) drawerConsumer = null;
}}
>
	<Sheet.Content side="right">
		{#if drawerConsumer}
			<Sheet.Header>
				<Sheet.Title>Consumer {drawerConsumer.name}</Sheet.Title>
				<Sheet.Description>
					One consumer on <span class="mono">{stream}</span> — pending is the pressure signal, redelivered
					the wedge signal, staleness judged against the monitor's clock.
				</Sheet.Description>
			</Sheet.Header>
			<div class="drawer-body">
				<dl class="record">
					<dt>stream</dt>
					<dd class="mono">{stream}</dd>
					<dt>service</dt>
					<dd class="mono">{drawerConsumer.service}</dd>
					<dt>consumer</dt>
					<dd class="mono">
						{drawerConsumer.durable ? drawerConsumer.name : `${drawerConsumer.name} (ephemeral)`}
					</dd>
					<dt>deliver group</dt>
					<dd class="mono">{drawerConsumer.deliver_group ?? '—'}</dd>
					<dt>pending</dt>
					<dd class="mono">{drawerConsumer.num_pending}</dd>
					<dt>ack-pending</dt>
					<dd class="mono">{drawerConsumer.num_ack_pending}</dd>
					<dt>redelivered</dt>
					<dd class="mono" class:warn={drawerConsumer.num_redelivered > 0}>
						{drawerConsumer.num_redelivered}
					</dd>
					<dt>last active</dt>
					<dd class="mono">
						{when(drawerConsumer.last_active)}
						{#if isStale(drawerConsumer.last_active)}
							<span class="stalechip" title="No delivery activity for over 10 minutes">stale</span>
						{/if}
					</dd>
				</dl>
				<p class="diagnosis">
					{#if isStale(drawerConsumer.last_active)}
						No delivery activity for over 10 minutes — on an active fabric that usually means the
						subscriber stopped reading (wedged), even if its pod looks Ready.
					{:else if drawerConsumer.num_redelivered > 0}
						Redeliveries are the wedge signal: the app received these messages but did not ack them in
						time.
					{:else if drawerConsumer.num_pending > 0}
						Backlog exists but delivery is active — pressure, not a wedge.
					{:else}
						Healthy: no backlog, no redeliveries, recent delivery activity.
					{/if}
				</p>
				{#if stream === 'DLQ' || stream.startsWith('DLQ_')}
					<div class="drawer-links">
						<a class="btn jumplink" href="{base}/admin/dlq">
							<ExternalLink size={12} /> Open the DLQ ops panel
						</a>
					</div>
				{/if}
			</div>
		{/if}
	</Sheet.Content>
</Sheet.Root>

<style>
	.service {
		color: var(--ink);
	}
	.num {
		color: var(--faint);
	}
	.pend {
		color: var(--mut);
	}
	.warn {
		color: var(--warn, #d18b28);
	}
	/* Stale consumer: dimmed cells (still legible) + a warn-toned chip on the last-active cell. */
	.stale {
		opacity: 0.55;
	}
	.stalechip {
		display: inline-block;
		border: 1px solid color-mix(in srgb, var(--warn, #d18b28) 60%, var(--line));
		border-radius: var(--radius-sm);
		color: var(--warn, #d18b28);
		font-size: 10px;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		padding: 0 5px;
		margin-left: 6px;
		opacity: 1;
	}
	.faint {
		color: var(--faint);
		white-space: nowrap;
	}
	.drawer-body {
		display: flex;
		flex-direction: column;
		gap: 14px;
		padding: 0 16px 16px;
		overflow-y: auto;
	}
	.record {
		display: grid;
		grid-template-columns: 96px 1fr;
		gap: 6px 10px;
		margin: 0;
		font-size: 12px;
	}
	.record dt {
		color: var(--faint);
		text-transform: uppercase;
		letter-spacing: 0.05em;
		font-size: 10.5px;
	}
	.record dd {
		margin: 0;
		color: var(--ink);
		word-break: break-all;
	}
	.diagnosis {
		color: var(--mut);
		font-size: 12px;
		margin: 0;
	}
	.drawer-links {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 8px;
	}
	.btn {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 12px;
		cursor: pointer;
	}
	.jumplink {
		text-decoration: none;
	}
	.mono {
		font-family: ui-monospace, monospace;
	}
</style>
