<script lang="ts">
	// The table-detail PREVIEW tab (goal cond 5): the first N rows through the explicit POST BFF
	// route onto the catalog's /query (Arrow-IPC file), parsed browser-side with apache-arrow and
	// rendered as a typed grid on the shared @rask/ui DataTable. Blob cells render an honest
	// "<blob NNkB>" chip (never megabytes of base64 soup); vector/list cells truncate with their
	// length stated. Empty, denied, signed-out, offline and parse-drift states are all explicit.
	import {
		createSvelteTable,
		DataTable,
		getCoreRowModel,
		getPaginationRowModel,
		renderSnippet,
		type ColumnDef,
		type PaginationState,
	} from '@rask/ui/data-table';
	import { RefreshCw, ShieldAlert } from '@lucide/svelte';
	import { untrack } from 'svelte';
	import { page } from '$app/state';
	import { tableFromIPC } from 'apache-arrow';
	import { lineageTick, liveRead } from '$lib/live/tick.svelte';
	import { queryTableRows } from './catalog';

	let { table }: { table: string } = $props();

	const DEFAULT_LIMIT = 50;
	const MAX_LIMIT = 500;

	// Return here after the OIDC round-trip (the shell's ?redirect= contract, nav-user.svelte).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	type Field = { name: string; type: string };
	type Row = Record<string, unknown>;

	let limit = $state<number | null>(DEFAULT_LIMIT);
	let fields = $state<Field[]>([]);
	let rows = $state<Row[] | null>(null); // null = not loaded yet (loading or failed)
	let lastStatus = $state(0);
	let parseError = $state<string | null>(null);
	let loading = $state(true);

	const unauthorized = $derived(rows === null && !loading && lastStatus === 401);
	const denied = $derived(rows === null && !loading && lastStatus === 403);
	const offline = $derived(
		rows === null && !loading && parseError === null && ![200, 401, 403].includes(lastStatus),
	);

	async function load(): Promise<void> {
		// Latest-wins: the route reuses this instance across table navigation — drop a stale response.
		const requested = table;
		const n = Math.min(Math.max(limit ?? DEFAULT_LIMIT, 1), MAX_LIMIT);
		loading = true;
		parseError = null;
		const res = await queryTableRows(requested, n);
		if (table !== requested) return;
		if (res.ok) {
			try {
				const arrow = tableFromIPC(new Uint8Array(res.data));
				fields = arrow.schema.fields.map((f): Field => ({ name: f.name, type: String(f.type) }));
				const children = arrow.schema.fields.map((f) => arrow.getChild(f.name));
				rows = Array.from({ length: arrow.numRows }, (_, i): Row =>
					Object.fromEntries(arrow.schema.fields.map((f, j) => [f.name, children[j]?.get(i)])),
				);
				lastStatus = 200;
			} catch (err) {
				// a body that is not the Arrow-IPC file the contract promises — surfaced, never guessed at
				rows = null;
				parseError = `preview response is not readable Arrow IPC: ${String(err)}`;
			}
		} else {
			rows = null;
			lastStatus = res.status;
		}
		loading = false;
	}

	$effect(() => {
		// Reset + reload on table change (limit changes go through the explicit Load button).
		void table;
		fields = [];
		rows = null;
		lastStatus = 0;
		parseError = null;
		limit = DEFAULT_LIMIT;
		// untracked: load() READS `limit`, and a tracked read would re-run this whole reset (and
		// clobber the user's typed limit back to the default) on every limit keystroke.
		untrack(() => load());
	});

	// #73: the Arrow preview was read ONCE per table and never again, so a compaction, a mover run or
	// anyone else's write left it showing rows that no longer exist — silently, because a stale table
	// looks exactly like a fresh one. It rides the SAME `lineageFeed` the shell's bell does, so the
	// preview and the notification above it can never disagree about when the estate changed.
	//
	// `load()` alone, deliberately NOT the reset `$effect` above: a cursor tick is not a table change,
	// so re-running the reset would clobber a limit the user typed back to the default while they were
	// reading. Same latest-wins guard applies — `load()` drops its own stale response.
	liveRead(lineageTick, () => load());

	/** Human form of one Arrow cell value. BigInt (int64), Date (timestamps) and typed-array /
	 * arrow-Vector cells (embeddings, lists) all reach here — a vector prints its first few
	 * elements plus its true length instead of flooding the grid. */
	function display(value: unknown): string {
		if (value == null) return '—';
		if (typeof value === 'bigint') return value.toString();
		if (typeof value === 'string') return value;
		if (typeof value === 'number' || typeof value === 'boolean') return String(value);
		if (value instanceof Date) return value.toISOString();
		const arrayLike = toArrayLike(value);
		if (arrayLike !== null) {
			const head = Array.from(arrayLike.slice(0, 4), (v) => display(v)).join(', ');
			return arrayLike.length > 4
				? `[${head}, …] (${arrayLike.length})`
				: `[${head}] (${arrayLike.length})`;
		}
		try {
			return JSON.stringify(value, (_k, v: unknown) => (typeof v === 'bigint' ? v.toString() : v));
		} catch {
			return String(value);
		}
	}

	/** Typed arrays (Float32Array vectors) and arrow Vectors (list cells) → a sliceable view. */
	function toArrayLike(
		value: unknown,
	): { length: number; slice(a: number, b: number): unknown[] } | null {
		if (ArrayBuffer.isView(value) && !(value instanceof DataView)) {
			const typed = value as unknown as {
				length: number;
				slice(a: number, b: number): ArrayLike<unknown>;
			};
			return { length: typed.length, slice: (a, b) => Array.from(typed.slice(a, b)) };
		}
		if (Array.isArray(value)) {
			return { length: value.length, slice: (a, b) => value.slice(a, b) };
		}
		const vec = value as { length?: unknown; toArray?: unknown; get?: unknown };
		if (
			typeof vec === 'object' &&
			typeof vec.length === 'number' &&
			typeof vec.get === 'function' &&
			typeof vec.toArray === 'function'
		) {
			const v = vec as { length: number; get(i: number): unknown };
			return {
				length: v.length,
				slice: (a, b) => Array.from({ length: Math.min(b, v.length) - a }, (_, i) => v.get(a + i)),
			};
		}
		return null;
	}

	function fmtKb(bytes: number): string {
		const kb = bytes / 1024;
		return `${kb >= 10 ? Math.round(kb) : Number(kb.toFixed(1))}kB`;
	}

	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: 10 });

	const columns = $derived(
		fields.map((f): ColumnDef<Row> => ({
			id: f.name,
			accessorFn: (r) => r[f.name],
			header: () => renderSnippet(headerCell, f),
			cell: ({ row }) => renderSnippet(valueCell, row.original[f.name]),
		})),
	);

	const grid = createSvelteTable({
		get data() {
			return rows ?? [];
		},
		get columns() {
			return columns;
		},
		state: {
			get pagination() {
				return pagination;
			},
		},
		onPaginationChange: (u) => (pagination = typeof u === 'function' ? u(pagination) : u),
		getCoreRowModel: getCoreRowModel(),
		getPaginationRowModel: getPaginationRowModel(),
	});
</script>

{#snippet headerCell(f: Field)}
	<span class="head mono">{f.name} <span class="type">{f.type}</span></span>
{/snippet}
{#snippet valueCell(value: unknown)}
	{#if value instanceof Uint8Array}
		<!-- an honest size chip — the bytes are opaque; the Blob-preview section renders images -->
		<span class="blobchip mono">&lt;blob {fmtKb(value.byteLength)}&gt;</span>
	{:else}
		<span class="cell mono">{display(value)}</span>
	{/if}
{/snippet}

<section>
	<h2>Preview</h2>
	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>
				This stack is governed — <a href={loginHref} data-sveltekit-reload>sign in</a> to preview rows.
			</p>
		</div>
	{:else if denied}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>You don't have read access to this table's data (can_read_data).</p>
		</div>
	{:else if offline}
		<div class="empty">
			<RefreshCw size={16} />
			<p>Catalog unreachable (HTTP {lastStatus}).</p>
			<button class="btn" disabled={loading} onclick={load}>Retry</button>
		</div>
	{:else if parseError}
		<div class="empty">
			<p class="error">{parseError}</p>
			<button class="btn" disabled={loading} onclick={load}>Retry</button>
		</div>
	{:else}
		<div class="toolbar">
			<label class="mut"
				>rows <input
					class="mono"
					type="number"
					min="1"
					max={MAX_LIMIT}
					bind:value={limit}
					aria-label="Preview row limit"
				/></label
			>
			<button class="btn" disabled={loading} onclick={load}>{loading ? '…' : 'Load'}</button>
		</div>
		<DataTable
			table={grid}
			{loading}
			emptyMessage="No rows — the table is empty at its current version."
		/>
	{/if}
</section>

<style>
	h2 {
		font-size: 13px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--faint);
		margin: 0 0 8px;
	}
	.toolbar {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-bottom: 10px;
		font-size: 12px;
	}
	.toolbar label {
		display: inline-flex;
		align-items: center;
		gap: 6px;
	}
	.toolbar input {
		width: 80px;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		padding: 3px 8px;
		font-size: 12px;
	}
	.btn {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 3px 10px;
		cursor: pointer;
	}
	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.head {
		font-size: 12px;
	}
	.head .type {
		color: var(--faint);
		font-weight: 400;
	}
	.cell {
		font-size: 12px;
		display: inline-block;
		max-width: 260px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		vertical-align: bottom;
	}
	.blobchip {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		padding: 0 7px;
		font-size: 12px;
		color: var(--mut);
	}
	.mut {
		color: var(--faint);
		font-size: 12px;
	}
	.error {
		color: var(--fail);
		font-size: 12px;
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--mut);
		padding: 24px 0;
	}
</style>
