<script lang="ts">
	// The FGA workbench's Tuples tab — the raw relationship store on the shared DataTable: filter by
	// object_type / user / object (server-side, against GET /v1/access/tuples), page through with the
	// continuation token, grant via a dialog (POST) and revoke per row behind a confirm (DELETE).
	// Estate-admin gated BY THE CATALOG; the BFF only bearer-forwards the session.
	import {
		createSvelteTable,
		DataTable,
		DataTableHeaderButton,
		DataTableTextFilter,
		getCoreRowModel,
		getFilteredRowModel,
		getPaginationRowModel,
		getSortedRowModel,
		renderComponent,
		renderSnippet,
		type ColumnDef,
		type PaginationState,
		type SortingState,
	} from '@rask/ui/data-table';
	import { AlertDialog } from '@rask/ui/alert-dialog';
	import { Dialog } from '@rask/ui/dialog';
	import { Plus, RefreshCw, ShieldAlert, Trash2 } from '@lucide/svelte';
	import { parse } from '@rask/api';
	import { untrack } from 'svelte';
	import { page } from '$app/state';
	import { deleteTuple, fetchTuples, type Tuple, TuplesPageSchema, writeTuple } from './access';

	// A parent can prefill the object filter (e.g. the Graph tab's "list this object's tuples" jump).
	let { object = '' }: { object?: string } = $props();

	// Return here after the OIDC round-trip (the shell's ?redirect= contract, nav-user.svelte).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	const PAGE_SIZE = 100;

	let tuples = $state<Tuple[] | null>(null);
	let continuation = $state<string | null>(null);
	let lastStatus = $state(0);
	let lastDetail = $state('');
	let settled = $state(false);
	let inflight = 0;
	let msg = $state<{ ok: boolean; text: string } | null>(null);

	// Server-side filters (applied on Search — typing must not hammer the tuple store per keystroke).
	let fObjectType = $state('');
	let fUser = $state('');
	// Initial-only capture BY DESIGN: the tab remounts per open, and the filter is the user's after that.
	// svelte-ignore state_referenced_locally
	let fObject = $state(object);

	// A bare user filter is rejected by the API by design (an OpenFGA Read needs an object type), so
	// the form refuses to send one: Search disables and the hint says what to add.
	const userOnly = $derived(!!fUser.trim() && !fObjectType.trim() && !fObject.trim());

	const unauthorized = $derived(tuples === null && settled && lastStatus === 401);
	const forbidden = $derived(tuples === null && settled && lastStatus === 403);
	const drifted = $derived(tuples === null && settled && lastStatus === -1);
	// 400 is the contract talking (bad filter shape), not the store being down — render its detail.
	const invalid = $derived(tuples === null && settled && lastStatus === 400);
	const offline = $derived(
		tuples === null && settled && ![-1, 200, 400, 401, 403].includes(lastStatus),
	);

	function filter(continuationToken?: string) {
		return {
			...(fObjectType.trim() ? { objectType: fObjectType.trim() } : {}),
			...(fUser.trim() ? { user: fUser.trim() } : {}),
			...(fObject.trim() ? { object: fObject.trim() } : {}),
			pageSize: PAGE_SIZE,
			...(continuationToken ? { continuation: continuationToken } : {}),
		};
	}

	async function load(): Promise<void> {
		if (userOnly) return; // the Search button is disabled too — never send a user-only filter
		const seq = ++inflight;
		const res = await fetchTuples(filter());
		if (seq !== inflight) return; // latest-wins
		settled = true;
		if (res.ok) {
			try {
				const parsed = parse(TuplesPageSchema, res.data);
				tuples = parsed.tuples;
				continuation = parsed.continuation;
				lastStatus = 200;
			} catch (err) {
				console.error(`tuples parse failure: ${String(err)}`);
				tuples = null;
				lastStatus = -1;
			}
		} else {
			// Clear stale rows on failure so the auth/forbidden/offline state reflects reality.
			tuples = null;
			lastStatus = res.status;
			lastDetail = res.detail;
		}
	}

	async function loadMore(): Promise<void> {
		if (!continuation) return;
		const seq = ++inflight;
		const res = await fetchTuples(filter(continuation));
		if (seq !== inflight || !res.ok) return;
		try {
			const parsed = parse(TuplesPageSchema, res.data);
			tuples = [...(tuples ?? []), ...parsed.tuples];
			continuation = parsed.continuation;
		} catch (err) {
			console.error(`tuples parse failure: ${String(err)}`);
		}
	}

	// Load once on mount, filters read UNTRACKED (AuditViewer's idiom): typing in the filter inputs
	// must not re-fire an estate-gated FGA read + audit event per keystroke — filters apply on the
	// explicit Search submit.
	$effect(() => {
		untrack(() => load());
	});

	// ── grant dialog (POST /v1/access/tuples) ──
	let grantOpen = $state(false);
	let gUser = $state('');
	let gRelation = $state('');
	let gObject = $state('');
	let busy = $state(false);

	async function grant(): Promise<void> {
		const tuple = { user: gUser.trim(), relation: gRelation.trim(), object: gObject.trim() };
		if (busy || !tuple.user || !tuple.relation || !tuple.object) return;
		busy = true;
		msg = null;
		try {
			const res = await writeTuple(tuple);
			if (res.ok) {
				msg = { ok: true, text: `Wrote (${tuple.user}, ${tuple.relation}, ${tuple.object}).` };
				grantOpen = false;
				gUser = gRelation = gObject = '';
				await load();
			} else if (res.status === 401) {
				msg = { ok: false, text: 'Sign in to write a tuple — grants are per-user actions.' };
			} else if (res.status === 403) {
				msg = { ok: false, text: 'Denied: tuple writes are estate-admin only.' };
			} else {
				msg = { ok: false, text: res.detail };
			}
		} finally {
			busy = false;
		}
	}

	// ── revoke (DELETE /v1/access/tuples, behind an explicit confirm) ──
	let revokeTarget = $state<Tuple | null>(null);

	async function revoke(): Promise<void> {
		const tuple = revokeTarget;
		if (busy || !tuple) return;
		busy = true;
		msg = null;
		try {
			const res = await deleteTuple(tuple);
			if (res.ok) {
				msg = { ok: true, text: `Revoked (${tuple.user}, ${tuple.relation}, ${tuple.object}).` };
				await load();
			} else if (res.status === 401) {
				msg = { ok: false, text: 'Sign in to revoke a tuple.' };
			} else if (res.status === 403) {
				msg = { ok: false, text: 'Denied: tuple writes are estate-admin only.' };
			} else {
				msg = { ok: false, text: res.detail };
			}
		} finally {
			busy = false;
			revokeTarget = null;
		}
	}

	// ── the DataTable: client-side sort/search over the loaded window ──
	let sorting = $state<SortingState>([]);
	let globalFilter = $state('');
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

	const columns: ColumnDef<Tuple>[] = [
		{
			id: 'user',
			accessorKey: 'user',
			header: sortableHeader('user'),
			meta: { cellClass: 'font-mono' },
		},
		{
			id: 'relation',
			accessorKey: 'relation',
			header: sortableHeader('relation'),
			meta: { cellClass: 'font-mono' },
		},
		{
			id: 'object',
			accessorKey: 'object',
			header: sortableHeader('object'),
			meta: { cellClass: 'font-mono' },
		},
		{
			id: 'actions',
			header: '',
			cell: ({ row }) => renderSnippet(actionsCell, row.original),
			meta: { headerClass: 'w-24', cellClass: 'text-right' },
		},
	];

	const table = createSvelteTable({
		get data() {
			return tuples ?? [];
		},
		columns,
		state: {
			get sorting() {
				return sorting;
			},
			get globalFilter() {
				return globalFilter;
			},
			get pagination() {
				return pagination;
			},
		},
		onSortingChange: (u) => (sorting = typeof u === 'function' ? u(sorting) : u),
		onGlobalFilterChange: (u) => (globalFilter = typeof u === 'function' ? u(globalFilter) : u),
		onPaginationChange: (u) => (pagination = typeof u === 'function' ? u(pagination) : u),
		getCoreRowModel: getCoreRowModel(),
		getSortedRowModel: getSortedRowModel(),
		getFilteredRowModel: getFilteredRowModel(),
		getPaginationRowModel: getPaginationRowModel(),
	});
</script>

{#snippet actionsCell(t: Tuple)}
	<button
		class="btn ghost"
		disabled={busy}
		aria-label="Revoke {t.user} {t.relation} {t.object}"
		onclick={() => (revokeTarget = t)}><Trash2 size={12} /> Revoke</button
	>
{/snippet}

<div class="tuples">
	<form
		class="filters"
		onsubmit={(e) => {
	e.preventDefault();
	load();
}}
	>
		<input
			class="mono"
			bind:value={fObjectType}
			placeholder="object_type (e.g. table)"
			aria-label="Object type filter"
		/>
		<input
			class="mono"
			bind:value={fUser}
			placeholder="user (e.g. user:alice)"
			aria-label="User filter"
		/>
		<input
			class="mono"
			bind:value={fObject}
			placeholder="object (e.g. table:db1$t)"
			aria-label="Object filter"
		/>
		<button class="btn" type="submit" disabled={userOnly}>Search</button>
		<button class="btn grant" type="button" onclick={() => (grantOpen = true)}>
			<Plus size={13} /> Grant
		</button>
	</form>

	{#if userOnly}
		<p class="hint">
			A user filter needs an object_type or object alongside it — the store reads per object type.
		</p>
	{/if}

	{#if msg}<p class="msg" class:okmsg={msg.ok} class:error={!msg.ok}>{msg.text}</p>{/if}

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={15} /> <a href={loginHref} data-sveltekit-reload>Sign in</a> to browse tuples.
		</div>
	{:else if forbidden}
		<div class="empty">
			<ShieldAlert size={15} /> The tuple store is estate-admin only — it maps every grant in the estate.
		</div>
	{:else if drifted}
		<div class="empty">
			<ShieldAlert size={15} /> The tuples payload drifted from the contract — refusing to render it.
		</div>
	{:else if invalid}
		<div class="empty"><ShieldAlert size={15} /> The store rejected the filter: {lastDetail}</div>
	{:else if offline}
		<div class="empty"><RefreshCw size={15} /> Tuple store unreachable (HTTP {lastStatus}).</div>
	{:else}
		<div class="toolbar">
			<DataTableTextFilter bind:value={globalFilter} placeholder="Search this window…" />
			{#if tuples !== null}
				<span class="count mono"
					>{tuples.length} tuple{tuples.length === 1 ? '' : 's'}{continuation
						? ' · more exist'
						: ''}</span
				>
			{/if}
		</div>
		<DataTable
			{table}
			loading={tuples === null}
			emptyMessage="No tuples match this filter — the store answers honestly, not everything is granted."
		/>
		{#if continuation}
			<button class="btn more" onclick={loadMore}>Load more</button>
		{/if}
	{/if}
</div>

<Dialog.Root bind:open={grantOpen}>
	<Dialog.Content>
		<Dialog.Title>Grant a tuple</Dialog.Title>
		<Dialog.Description>
			Writes one relationship tuple to the live store (estate-admin gated by the catalog). The model
			itself stays read-only — this grants within it.
		</Dialog.Description>
		<form
			class="grant-form"
			onsubmit={(e) => {
	e.preventDefault();
	grant();
}}
		>
			<input
				class="mono"
				bind:value={gUser}
				placeholder="user (e.g. user:alice or team:eng#member)"
				aria-label="Grant user"
			/>
			<input
				class="mono"
				bind:value={gRelation}
				placeholder="relation (e.g. reader)"
				aria-label="Grant relation"
			/>
			<input
				class="mono"
				bind:value={gObject}
				placeholder="object (e.g. table:db1$t)"
				aria-label="Grant object"
			/>
			<div class="dialog-actions">
				<button class="btn" type="button" disabled={busy} onclick={() => (grantOpen = false)}>
					Cancel
				</button>
				<button
					class="btn grant"
					type="submit"
					disabled={busy || !gUser.trim() || !gRelation.trim() || !gObject.trim()}
				>
					{busy ? '…' : 'Write tuple'}
				</button>
			</div>
		</form>
	</Dialog.Content>
</Dialog.Root>

<AlertDialog.Root
	open={revokeTarget !== null}
	onOpenChange={(open) => {
	if (!open) revokeTarget = null;
}}
>
	<AlertDialog.Content>
		<AlertDialog.Title>Revoke this tuple</AlertDialog.Title>
		<AlertDialog.Description>
			{#if revokeTarget}
				Deletes <span class="mono"
					>({revokeTarget.user}, {revokeTarget.relation}, {revokeTarget.object})</span
				> from the live store. Whatever this tuple granted stops immediately.
			{/if}
		</AlertDialog.Description>
		<div class="dialog-actions">
			<AlertDialog.Cancel disabled={busy}>Cancel</AlertDialog.Cancel>
			<AlertDialog.Action
				class="border-destructive/40 bg-destructive/15 text-destructive hover:bg-destructive/25"
				disabled={busy}
				onclick={revoke}
			>
				Revoke
			</AlertDialog.Action>
		</div>
	</AlertDialog.Content>
</AlertDialog.Root>

<style>
	.tuples {
		display: flex;
		flex-direction: column;
		gap: 10px;
	}
	.filters {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
	}
	.filters input,
	.grant-form input {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 8px;
		flex: 1 1 160px;
		min-width: 130px;
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
	.btn.ghost {
		padding: 2px 8px;
		background: none;
		color: var(--mut);
	}
	.btn.grant {
		border-color: color-mix(in srgb, var(--ok) 45%, var(--line));
		color: var(--ok);
	}
	.btn.more {
		align-self: flex-start;
	}
	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.toolbar {
		display: flex;
		align-items: center;
		gap: 10px;
	}
	.count {
		color: var(--faint);
		font-size: 11px;
	}
	.msg {
		font-size: 12px;
		margin: 0;
	}
	.hint {
		color: var(--warn, #d18b28);
		font-size: 11px;
		margin: 0;
	}
	.okmsg {
		color: var(--ok);
	}
	.error {
		color: var(--fail);
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--mut);
		font-size: 13px;
		padding: 24px 0;
	}
	.grant-form {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.dialog-actions {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
		margin-top: 4px;
	}
	.mono {
		font-family: ui-monospace, monospace;
	}
</style>
