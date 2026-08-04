<script lang="ts">
	// A2 · the task queue, and A3 · review — one table, because they are one loop seen from two
	// roles. Every action button here renders from the task's OWN `legal_events` (supplied by the
	// backend from the machine tables) — the UI holds no second copy of the state machine. A
	// denial is the SERVER's 403 surfaced with its reason; a disabled button is never the gate.
	import { base } from '$app/paths';
	import {
		createSvelteTable,
		DataTable,
		DataTableHeaderButton,
		getCoreRowModel,
		getPaginationRowModel,
		getSortedRowModel,
		renderComponent,
		renderSnippet,
		selectionColumn,
		type ColumnDef,
		type PaginationState,
		type RowSelectionState,
		type SortingState,
	} from '@rask/ui/data-table';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { Dialog } from '@rask/ui/dialog';
	import { Input } from '@rask/ui/input';
	import { Textarea } from '@rask/ui/textarea';
	import { ExternalLink, Trash2 } from '@lucide/svelte';

	import LeaseChip from './LeaseChip.svelte';
	import { dropTask } from './remote/projects.remote';
	import { fireTaskEvent } from './remote/tasks.remote';
	import { EVENT_LABELS, taskStateVariant } from './presentation.js';
	import type { Adjudication, TaskDetail } from './types.js';

	let {
		projectId,
		tasks,
		me,
		consensusN = 1,
		adjudications = {},
		droppable = false,
		onchanged,
	}: {
		projectId: string;
		tasks: TaskDetail[];
		me: string | null;
		/** The labeling task's `consensus_n` — labels the replica chip ("replica k/N"). */
		consensusN?: number;
		/** Group id → the manager's canonical pick — marks the winning replica in the queue. */
		adjudications?: Record<string, Adjudication>;
		/** Fired after any successful task event so the page refetches ONE snapshot. */
		onchanged: () => void;
		/** Whether this project can still have items removed (draft/labeling). The SERVER re-checks
		 *  both the permission and the state; hiding the button is presentation, not authorization. */
		droppable?: boolean;
	} = $props();

	let busy = $state<string | null>(null); // `${task_id}:${event}` in flight
	let notice = $state<{ ok: boolean; text: string } | null>(null);

	// request_changes carries a reviewer message (the ReviewNote §5.2 appends) — dialog state.
	let changesFor = $state<TaskDetail | null>(null);
	let changesMessage = $state('');

	// assign names a RECIPIENT (the manager's distribution edge — §5.2: pinned lease, never
	// expires). The recipient field is safe as input because the caller's own authority is still
	// can_manage, checked server-side against the item's labeling task.
	let assignFor = $state<TaskDetail | null>(null);
	let assignee = $state('');

	async function fire(
		task: TaskDetail,
		event: string,
		opts?: { message?: string; assignee?: string },
	): Promise<void> {
		if (busy) return;
		busy = `${task.task_id}:${event}`;
		notice = null;
		const result = await fireTaskEvent({ taskId: task.task_id, event, ...opts });
		busy = null;
		if (result.ok) {
			onchanged();
			return;
		}
		// The server's refusal IS the surface: 403 names the closed door (self-review, not the
		// holder, missing rung), 409 names the illegal edge or a lost race. Show it verbatim.
		notice = { ok: false, text: result.detail };
	}

	// ── bulk review: the LLM-as-judge foundation ──────────────────────────────────────────────
	// A model-labelled batch lands its tasks in in_review; the reviewer selects and accepts en
	// masse. Server-gated per task exactly like a single accept — a selected row the machine
	// refuses (not in_review, self-review) FAILS individually and is reported, never skipped
	// silently. Row ids are task ids, so selection survives refetches.
	let rowSelection = $state<RowSelectionState>({});
	const selectedIds = $derived(Object.keys(rowSelection).filter((id) => rowSelection[id]));
	const selectedReviewable = $derived(
		tasks.filter((t) => selectedIds.includes(t.task_id) && t.state === 'in_review'),
	);
	let bulkBusy = $state(false);

	async function bulkAccept(): Promise<void> {
		// SNAPSHOT the derived at entry: clearing the selection below re-derives it to [], and a
		// completion notice computed from the post-clear value reports "Accepted 0" for real work
		// (the bulk e2e caught exactly that).
		const targets = [...selectedReviewable];
		if (bulkBusy || targets.length === 0) return;
		bulkBusy = true;
		notice = null;
		const failures: string[] = [];
		for (const task of targets) {
			const result = await fireTaskEvent({ taskId: task.task_id, event: 'accept' });
			if (!result.ok) failures.push(`${task.source.keys[0] ?? task.task_id}: ${result.detail}`);
		}
		bulkBusy = false;
		rowSelection = {};
		notice =
			failures.length === 0
				? { ok: true, text: `Accepted ${targets.length} item${targets.length === 1 ? '' : 's'}.` }
				: { ok: false, text: `${failures.length} of ${targets.length} refused — ${failures[0]}` };
		onchanged();
	}

	function canvasHref(task: TaskDetail): string {
		const keys = task.source.keys.join(',');
		const dataset = task.source.where ? `dataset=${encodeURIComponent(task.source.where)}&` : '';
		return `${base}/?${dataset}keys=${encodeURIComponent(keys)}&task=${task.task_id}`;
	}

	/** The task's own legal events, minus `save_draft` (that belongs to the canvas). `assign`
	 *  renders as its own dialog-opening button below. */
	function rowEvents(task: TaskDetail): string[] {
		return task.legal_events
			.map((e) => e.event)
			.filter((e) => e !== 'assign' && e !== 'save_draft');
	}

	/** Remove an item that can never be completed. Confirmed, because it discards queued work and
	 *  there is no undo — the task's own document survives, but nothing lists it any more. */
	async function remove(task: TaskDetail): Promise<void> {
		if (busy !== null) return;
		if (!confirm(`Remove this item from the project? Its queued work is discarded.`)) return;
		busy = `${task.task_id}:drop`;
		notice = null;
		const result = await dropTask({ projectId, taskId: task.task_id });
		busy = null;
		// The SERVER's words — a 409 past `labeling`, a 403, or its own report. "Remove failed" would
		// tell the manager nothing about which of those happened.
		notice = result.ok
			? { ok: true, text: result.data.removed ? 'Item removed.' : 'That item was already gone.' }
			: { ok: false, text: result.detail };
		if (result.ok) onchanged();
	}

	function canAssign(task: TaskDetail): boolean {
		return task.legal_events.some((e) => e.event === 'assign');
	}

	/** Consensus v1: "replica k/N" from the deterministic sibling id (`{group}-r{k}`). Returns
	 *  null for an ordinary item, so the chip renders only where replicas exist. */
	function replicaLabel(task: TaskDetail): string | null {
		if (!task.replica_of) return null;
		const k = /-r(\d+)$/.exec(task.task_id)?.[1];
		return k ? `replica ${k}/${consensusN}` : `replica of ${task.replica_of}`;
	}

	let sorting = $state<SortingState>([{ id: 'state', desc: false }]);
	let pagination = $state<PaginationState>({ pageIndex: 0, pageSize: 20 });

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

	const columns: ColumnDef<TaskDetail>[] = [
		selectionColumn<TaskDetail>(),
		{
			id: 'item',
			accessorFn: (t) => t.source.keys.join(','),
			header: sortableHeader('item'),
			cell: ({ row }) => renderSnippet(itemCell, row.original),
			meta: { cellClass: 'whitespace-nowrap' },
		},
		{
			id: 'state',
			accessorKey: 'state',
			header: sortableHeader('state'),
			cell: ({ row }) => renderSnippet(stateCell, row.original),
		},
		{
			id: 'lease',
			accessorFn: (t) => t.assignee ?? '',
			header: 'lease',
			cell: ({ row }) => renderSnippet(leaseCell, row.original),
		},
		{
			id: 'provenance',
			accessorFn: (t) => t.submitted_by ?? '',
			header: 'submitted / reviewed',
			cell: ({ row }) => renderSnippet(provenanceCell, row.original),
		},
		{
			id: 'actions',
			header: '',
			cell: ({ row }) => renderSnippet(actionsCell, row.original),
			meta: { cellClass: 'text-right' },
		},
	];

	const table = createSvelteTable({
		get data() {
			return tasks;
		},
		columns,
		getRowId: (task) => task.task_id,
		state: {
			get sorting() {
				return sorting;
			},
			get pagination() {
				return pagination;
			},
			get rowSelection() {
				return rowSelection;
			},
		},
		enableRowSelection: true,
		onSortingChange: (u) => (sorting = typeof u === 'function' ? u(sorting) : u),
		onPaginationChange: (u) => (pagination = typeof u === 'function' ? u(pagination) : u),
		onRowSelectionChange: (u) => (rowSelection = typeof u === 'function' ? u(rowSelection) : u),
		getCoreRowModel: getCoreRowModel(),
		getSortedRowModel: getSortedRowModel(),
		getPaginationRowModel: getPaginationRowModel(),
	});
</script>

{#snippet itemCell(task: TaskDetail)}
	<!-- The chip sits UNDER the key, not beside it: the cell is whitespace-nowrap, and an inline
	     chip widened it until the actions column clipped (seen on the consensus screenshot). -->
	<div class="flex flex-col items-start gap-0.5">
		<span class="font-mono text-xs" title={task.task_id}
			>{task.source.keys.join(', ') || task.task_id}</span
		>
		{#if replicaLabel(task)}
			<span class="flex items-center gap-1">
				<Badge
					variant="outline"
					title="one of {consensusN} independent annotations of this item — group {task.replica_of}"
				>
					{replicaLabel(task)}
				</Badge>
				{#if task.replica_of && adjudications[task.replica_of]?.task_id === task.task_id}
					<Badge title="the manager's canonical pick for group {task.replica_of}">canonical</Badge>
				{/if}
			</span>
		{/if}
	</div>
{/snippet}

{#snippet stateCell(task: TaskDetail)}
	<Badge variant={taskStateVariant(task.state)}>{task.state.replace('_', ' ')}</Badge>
{/snippet}

{#snippet leaseCell(task: TaskDetail)}
	<LeaseChip {task} {me} />
{/snippet}

{#snippet provenanceCell(task: TaskDetail)}
	<span class="text-muted-foreground text-xs">
		{#if task.submitted_by}{task.submitted_by}{/if}
		{#if task.reviewed_by}
			· {task.review_action === 'fix_and_accept' ? 'fixed & accepted' : task.review_action} by {task.reviewed_by}
		{/if}
		{#if task.review_notes.length > 0}
			<span title={task.review_notes.map((n) => `${n.by}: ${n.message}`).join('\n')}>
				· {task.review_notes.length} note{task.review_notes.length === 1 ? '' : 's'}
			</span>
		{/if}
	</span>
{/snippet}

{#snippet actionsCell(task: TaskDetail)}
	<!-- flex-wrap: an in_review row carries three review buttons and the cell must stack them
	     rather than clip at the card edge (found by looking at the A2/A3 screenshots). -->
	<div class="flex flex-wrap items-center justify-end gap-1">
		{#if droppable}
			<!-- The exit an unfinishable item had none of. An item naming a media dataset that was
			     renamed or removed cannot be opened, so it can never reach a terminal state — and the
			     publish requires EVERY task terminal, so one of them wedges the project forever. -->
			<Button
				variant="ghost"
				size="icon-sm"
				disabled={busy !== null}
				data-testid="drop-task"
				title="remove this item from the project (it cannot be completed)"
				onclick={() => void remove(task)}
			>
				<Trash2 class="size-3.5" />
			</Button>
		{/if}
		{#if task.state === 'claimed'}
			<Button
				variant="outline"
				size="sm"
				href={canvasHref(task)}
				title="open this item on the annotate canvas"
			>
				<ExternalLink class="size-3.5" /> Annotate
			</Button>
		{/if}
		{#if canAssign(task)}
			<Button
				variant="outline"
				size="sm"
				disabled={busy !== null}
				title="assign this item to a named annotator (pinned — the lease never expires)"
				onclick={() => {
	assignFor = task;
	assignee = '';
}}
			>
				{EVENT_LABELS['assign']}
			</Button>
		{/if}
		{#each rowEvents(task) as event (event)}
			{#if event === 'request_changes'}
				<Button
					variant="outline"
					size="sm"
					disabled={busy !== null}
					title={task.submitted_by && me !== null && task.submitted_by === me
	? 'you submitted this — the server will refuse a self-review'
	: 'send the task back with a note'}
					onclick={() => {
	changesFor = task;
	changesMessage = '';
}}
				>
					{EVENT_LABELS[event] ?? event}
				</Button>
			{:else}
				<Button
					variant={event === 'accept' ? 'default' : 'outline'}
					size="sm"
					disabled={busy !== null}
					title={(event === 'accept' || event === 'fix_and_accept') &&
					task.submitted_by &&
					me !== null &&
					task.submitted_by === me
	? 'you submitted this — the server will refuse a self-review'
	: undefined}
					onclick={() => void fire(task, event)}
				>
					{EVENT_LABELS[event] ?? event}
				</Button>
			{/if}
		{/each}
	</div>
{/snippet}

<div class="flex flex-col gap-2">
	{#if selectedIds.length > 0}
		<div
			class="bg-muted/50 flex items-center gap-2 rounded-md border px-3 py-1.5 text-sm"
			data-testid="bulk-bar"
		>
			<span class="text-muted-foreground">
				{selectedIds.length} selected · {selectedReviewable.length} reviewable
			</span>
			<Button
				size="sm"
				class="ml-auto"
				disabled={bulkBusy || selectedReviewable.length === 0}
				onclick={() => void bulkAccept()}
			>
				{bulkBusy ? 'Accepting…' : `Accept ${selectedReviewable.length} reviewed`}
			</Button>
		</div>
	{/if}
	{#if notice}
		<p class={notice.ok ? 'text-success text-sm' : 'text-destructive text-sm'}>{notice.text}</p>
	{/if}
	<DataTable {table} emptyMessage="No items yet — send data points in from Search or the Atlas." />
</div>

<Dialog.Root
	open={assignFor !== null}
	onOpenChange={(open) => (assignFor = open ? assignFor : null)}
>
	<Dialog.Content class="sm:max-w-sm">
		<Dialog.Title>Assign this item</Dialog.Title>
		<Dialog.Description>
			The named annotator holds it with a pinned lease (it never expires) until they submit, release,
			or a manager reassigns.
		</Dialog.Description>
		<form
			class="flex flex-col gap-3"
			onsubmit={(e) => {
	e.preventDefault();
	const task = assignFor;
	assignFor = null;
	if (task && assignee.trim()) void fire(task, 'assign', { assignee: assignee.trim() });
}}
		>
			<Input bind:value={assignee} placeholder="annotator (OIDC subject or username)" />
			<div class="flex justify-end gap-2">
				<Button type="button" variant="outline" onclick={() => (assignFor = null)}>Cancel</Button>
				<Button type="submit" disabled={!assignee.trim()}>Assign</Button>
			</div>
		</form>
	</Dialog.Content>
</Dialog.Root>

<Dialog.Root
	open={changesFor !== null}
	onOpenChange={(open) => (changesFor = open ? changesFor : null)}
>
	<Dialog.Content class="sm:max-w-md">
		<Dialog.Title>Request changes</Dialog.Title>
		<Dialog.Description>
			The note travels with the task (an append-only review note) — say what should change.
		</Dialog.Description>
		<form
			class="flex flex-col gap-3"
			onsubmit={(e) => {
	e.preventDefault();
	const task = changesFor;
	changesFor = null;
	if (task) void fire(task, 'request_changes', { message: changesMessage });
}}
		>
			<Textarea
				bind:value={changesMessage}
				rows={4}
				placeholder="The stamp in the corner is unlabelled…"
			/>
			<div class="flex justify-end gap-2">
				<Button type="button" variant="outline" onclick={() => (changesFor = null)}>Cancel</Button>
				<Button type="submit" disabled={!changesMessage.trim()}>Request changes</Button>
			</div>
		</form>
	</Dialog.Content>
</Dialog.Root>
