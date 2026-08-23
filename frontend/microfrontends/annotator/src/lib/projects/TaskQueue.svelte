<script lang="ts">
	// A2 · the task queue, and A3 · review — one table, because they are one loop seen from two
	// roles. Every action button here renders from the task's OWN `legal_events` (supplied by the
	// backend from the machine tables) — the UI holds no second copy of the state machine. A
	// denial is the SERVER's 403 surfaced with its reason; a disabled button is never the gate.
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import { taskCanvasHref } from '$lib/viewer/task-stream';
	import {
		createSvelteTable,
		DataTable,
		getCoreRowModel,
		getPaginationRowModel,
		getSortedRowModel,
		renderSnippet,
		selectionColumn,
		type ColumnDef,
		type PaginationState,
		type RowSelectionState,
		type SortingState,
		sortableHeader,
	} from '@rask/ui/data-table';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { Dialog } from '@rask/ui/dialog';
	import { Input } from '@rask/ui/input';
	import { ResizableSplit } from '@rask/ui/resizable-split';
	import { Select } from '@rask/ui/select';

	import { bulkActions, targetsFor } from './bulk-events';
	import { indexRows, rowFor, rowKeysFor, rowText } from './corpus-rows';
	import { fetchCorpusRows } from './remote/rows.remote';
	import {
		corpusText,
		distinctValues,
		labelText,
		matchesText,
		mediaText,
		predictedLabels,
	} from './item-columns';
	import { Textarea } from '@rask/ui/textarea';
	import { Eye, ExternalLink, LayoutGrid, Rows3, Trash2 } from '@lucide/svelte';

	import ImportButton from './ImportButton.svelte';
	import TaskGrid from './TaskGrid.svelte';
	import { parseView, QUEUE_VIEW_KEY, type QueueView } from './queue-view';
	import { selectAllPrompt, selectionOf } from './select-all';
	import QuickView from './QuickView.svelte';
	import { afterVerdict } from './quick-view';
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

	// QUEUE FILTER. A project of a thousand items is unnavigable without one, which is why this is
	// the first campaign feature: it is what makes every other one demonstrable.
	//
	// Filtered here rather than through TanStack's column filters because `assignee` is not an
	// accessor column, and adding a hidden one to filter by it would be plumbing in service of the
	// framework rather than the problem. Filtering the INPUT means pagination, sorting and the
	// selection model all follow for free.
	let filterState = $state('');
	let filterAssignee = $state('');
	//: The ITEM filters. `filterText` is one box searching the key, corpus, label and modality
	//: together, because a person typing into one field means "find this anywhere" — asking which
	//: column it lives in is asking them to know the schema.
	let filterText = $state('');
	let filterLabel = $state('');

	// #60 — THE CORPUS ROW behind each item. A task carries its row KEYS, not the row, so until now
	// the queue could show the item's administration (state, assignee, lease) but never a fact about
	// the thing being labelled: a date, a page number, the transcribed text. "Filter to the 1890s"
	// was unanswerable, and `item-columns.ts` carried a standing note refusing to fake it.
	//
	// Fetched for the WHOLE task list, not the visible page, and that is load-bearing: filtering is
	// what these rows are for, and filtering a page by data only fetched for that page is circular —
	// you could never filter TO a row that is not already on screen. A labeling task is bounded by
	// SEND_TASK_CAP (1000), which is also the endpoint's own cap, so the two agree by construction.
	const rowsQuery = $derived(fetchCorpusRows({ keys: rowKeysFor(tasks), dataset: null }));
	// Rows are DECORATION on a queue that already works without them: `?? []` everywhere, never a
	// loading gate. An unreachable viewer costs the extra columns, not the queue.
	const rowIndex = $derived(
		indexRows(rowsQuery.current?.rows ?? [], rowsQuery.current?.keyFields ?? []),
	);
	const rowKeyFields = $derived(rowsQuery.current?.keyFields ?? []);

	const visible = $derived(
		tasks.filter(
			(t) =>
				(filterState === '' || t.state === filterState) &&
				(filterAssignee === '' ||
					(t.assignee ?? '').toLowerCase().includes(filterAssignee.trim().toLowerCase())) &&
				(filterLabel === '' ||
					labelText(t)
						.split(',')
						.some((l) => l.trim() === filterLabel)) &&
				// The corpus row is OR-ed with the task's own text rather than replacing it: one box
				// means "find this anywhere", and a person typing a doc id must still match the item
				// even when its row has not arrived (or never will — a task may outlive its row, #37).
				(matchesText(t, filterText) ||
					rowText(rowFor(t, rowIndex), rowKeyFields)
						.toLowerCase()
						.includes(filterText.trim().toLowerCase())),
		),
	);
	const filtering = $derived(
		filterState !== '' ||
			filterAssignee.trim() !== '' ||
			filterLabel !== '' ||
			filterText.trim() !== '',
	);

	/** The labels actually PRESENT, so the dropdown never offers a filter that yields nothing —
	 *  the same rule `statesPresent` already follows. */
	const labelsPresent = $derived(distinctValues(tasks, labelText));

	/** The states actually PRESENT, so the dropdown never offers a filter that yields nothing. */
	const statesPresent = $derived([...new Set(tasks.map((t) => t.state))].sort());

	/** Changing a filter clears the selection.
	 *
	 *  Not cosmetic: `rowSelection` is keyed by task id and survives a row leaving the visible set,
	 *  so filtering down, selecting all, then clearing the filter would leave rows selected that the
	 *  annotator never saw — and the bulk actions act on a selection. Cleared in the HANDLER rather
	 *  than an effect, because assigning state inside an effect is the anti-pattern that makes this
	 *  kind of coupling invisible.
	 */
	function onFilterChanged(): void {
		rowSelection = {};
	}

	/** LIST or GRID. A list is how you audit a queue; a grid is how you scan one — and for page
	 *  images the grid answers "which of these are letters" in a glance. Both read the SAME filtered,
	 *  sorted rows and the SAME selection, so they can never disagree about what is in view.
	 *
	 *  Seeded to `list` and restored on mount: `localStorage` does not exist during SSR, so reading it
	 *  at initialisation would crash the server render. */
	let view = $state<QueueView>('list');
	onMount(() => {
		try {
			view = parseView(localStorage.getItem(QUEUE_VIEW_KEY));
		} catch {
			// A browser refusing storage (private mode, blocked cookies) is not a reason to fail the
			// queue — it just means the preference does not persist.
		}
	});

	function setView(next: QueueView): void {
		view = next;
		try {
			localStorage.setItem(QUEUE_VIEW_KEY, next);
		} catch {}
	}

	/** QUICK VIEW — which item is open beside the queue, if any.
	 *
	 *  A pre-labelled queue is REVIEWED rather than drawn on, and doing that through the canvas route
	 *  costs the filter, the page and the selection on every round trip. */
	let quickId = $state<string | null>(null);
	let quickOpen = $state(false);

	function openQuick(task: TaskDetail): void {
		quickId = task.task_id;
		quickOpen = true;
	}

	/** Fire an event from the drawer, then ADVANCE — the point of a review pass is the next item.
	 *
	 *  Snapshotted BEFORE the call: `onchanged()` refetches, so computing the successor afterwards
	 *  would read a list in which this task may have moved or left. At the end of the page the
	 *  successor is null and the drawer closes, rather than looping. */
	async function fireFromQuick(task: TaskDetail, event: string): Promise<void> {
		const next = afterVerdict(pageRows, task.task_id);
		await fire(task, event);
		if (notice?.ok === false) return; // refused — stay on the item so the reason is readable
		quickId = next;
		quickOpen = next !== null;
	}

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
		const result = await fireTaskEvent({ taskId: task.task_id, event, projectId, ...opts });
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

	/** Selected rows the machine will actually accept an `assign` for.
	 *
	 *  Derived from each task's OWN `legal_events`, exactly like the per-row button — not from a
	 *  second guess at the state machine here. A row the machine refuses still FAILS individually and
	 *  is reported; this only stops the button offering work it can already see is impossible. */
	const selectedAssignable = $derived(
		tasks.filter((t) => selectedIds.includes(t.task_id) && canAssign(t)),
	);
	/** Non-null while the BULK assign dialog is open, holding the rows it will act on. */
	let bulkAssignFor = $state<TaskDetail[] | null>(null);

	/** Assign every selected item to one annotator.
	 *
	 *  ONE gated event per item, reported per item — the bulk-accept precedent beside this, and the
	 *  only shape the actor model can honour. There is no transaction across task actors, so a
	 *  rollback would be a second best-effort loop that can itself half-fail; claiming atomicity we
	 *  cannot deliver would be worse than reporting the truth.
	 */
	async function bulkAssign(targets: TaskDetail[], to: string): Promise<void> {
		// SNAPSHOT at entry, like bulkAccept: clearing the selection below re-derives the source to
		// [], and a completion notice computed after that reports "Assigned 0" for real work.
		const items = [...targets];
		if (bulkBusy || items.length === 0 || !to) return;
		bulkBusy = true;
		notice = null;
		const failures: string[] = [];
		for (const task of items) {
			const result = await fireTaskEvent({
				taskId: task.task_id,
				event: 'assign',
				assignee: to,
				projectId,
			});
			if (!result.ok) failures.push(`${task.source.keys[0] ?? task.task_id}: ${result.detail}`);
		}
		bulkBusy = false;
		rowSelection = {};
		// Names WHICH failed and how many landed. "Assign failed" would leave a manager unable to
		// tell a permission problem from a race on one row.
		notice =
			failures.length === 0
				? {
						ok: true,
						text: `Assigned ${items.length} item${items.length === 1 ? '' : 's'} to ${to}.`,
					}
				: {
						ok: false,
						text: `${items.length - failures.length} of ${items.length} assigned to ${to} — ${failures[0]}`,
					};
		onchanged();
	}

	/** Every action this SELECTION can perform, derived from the rows' own `legal_events`.
	 *
	 *  The queue used to offer exactly two — `accept` and `assign`, hand-picked — while every per-row
	 *  button came from the task machine. So the row menu could claim, submit, skip, release, requeue
	 *  and reopen, and the selection could not; claiming twenty items meant twenty clicks, which is
	 *  what made this read as "pick a row, do a thing" however good the selection model was. */
	const available = $derived(bulkActions(tasks, selectedIds));

	/** Fire one event across every selected row that declares it legal.
	 *
	 *  ONE gated call per item, reported per item — the shape the actor model forces. There is no
	 *  transaction across task actors, so a rollback would be a second best-effort loop that can
	 *  itself half-fail; claiming an atomicity we cannot deliver would be worse than reporting the
	 *  truth. Generalised from the old `bulkAccept`, which was this loop with `accept` baked in. */
	async function bulkFire(event: string): Promise<void> {
		// SNAPSHOT at entry: clearing the selection below re-derives the source to [], and a completion
		// notice computed after that reports "0 items" for real work (the bulk e2e caught exactly that).
		const targets = targetsFor(tasks, selectedIds, event);
		if (bulkBusy || targets.length === 0) return;
		if (event === 'skip' && !confirm(`Skip ${targets.length} item(s)? They leave the queue.`))
			return;
		bulkBusy = true;
		notice = null;
		const failures: string[] = [];
		for (const task of targets) {
			const result = await fireTaskEvent({ taskId: task.task_id, event, projectId });
			if (!result.ok) failures.push(`${task.source.keys[0] ?? task.task_id}: ${result.detail}`);
		}
		bulkBusy = false;
		rowSelection = {};
		const label = (EVENT_LABELS[event] ?? event).replace('…', '');
		// Names WHICH failed and how many landed. A bare "failed" leaves a manager unable to tell a
		// permission problem from a race on one row.
		notice =
			failures.length === 0
				? { ok: true, text: `${label}: ${targets.length} item${targets.length === 1 ? '' : 's'}.` }
				: {
						ok: false,
						text: `${targets.length - failures.length} of ${targets.length} — ${failures[0]}`,
					};
		onchanged();
	}

	/** Remove every selected item. Confirmed ONCE for the batch — a per-item confirm on forty rows is
	 *  a dialog nobody reads by the fourth one, which is worse than no confirm at all.
	 *
	 *  `dropTask` rather than `fireTaskEvent`: removing an item is a PROJECT operation (it leaves the
	 *  index), not an edge in the task's own machine, which is why it never appeared in `legal_events`
	 *  and so cannot come from `bulkActions`. */
	async function bulkRemove(): Promise<void> {
		const targets = tasks.filter((t) => selectedIds.includes(t.task_id));
		if (bulkBusy || targets.length === 0) return;
		if (
			!confirm(`Remove ${targets.length} item(s) from the project? Their queued work is discarded.`)
		)
			return;
		bulkBusy = true;
		notice = null;
		const failures: string[] = [];
		for (const task of targets) {
			const result = await dropTask({ projectId, taskId: task.task_id });
			if (!result.ok) failures.push(`${task.source.keys[0] ?? task.task_id}: ${result.detail}`);
		}
		bulkBusy = false;
		rowSelection = {};
		notice =
			failures.length === 0
				? { ok: true, text: `Removed ${targets.length} item${targets.length === 1 ? '' : 's'}.` }
				: {
						ok: false,
						text: `${targets.length - failures.length} of ${targets.length} removed — ${failures[0]}`,
					};
		onchanged();
	}

	function canvasHref(task: TaskDetail): string {
		// The ONE href builder (`taskCanvasHref`) — this was a second spelling of the same URL, and
		// it drifted exactly the way second spellings do: when the stream's links learned to carry
		// the task's modality (`kind=`), the queue's links didn't, so a text/audio/video task opened
		// its own canvas from the filmstrip and the image canvas from the queue. `project` rides
		// along because the canvas's exit needs to address the QUEUE PAGE, and a task id alone
		// cannot.
		return taskCanvasHref(task, projectId, base);
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

	const columns: ColumnDef<TaskDetail>[] = [
		selectionColumn<TaskDetail>(),
		{
			id: 'item',
			accessorFn: (t) => t.source.keys.join(','),
			header: sortableHeader('item'),
			cell: ({ row }) => renderSnippet(itemCell, row.original),
			meta: { cellClass: 'whitespace-nowrap' },
		},
		// ── the ITEM's own columns. Everything above and below this block is task ADMINISTRATION;
		//    these describe the thing being labelled, which is what decides what to work on next.
		{
			id: 'label',
			accessorFn: labelText,
			header: sortableHeader('label'),
			cell: ({ row }) => renderSnippet(labelCell, row.original),
		},
		{
			id: 'corpus',
			accessorFn: corpusText,
			header: sortableHeader('corpus'),
			// The version pin deliberately does NOT render here, though it belongs to this column's
			// subject. Showing it needs a `renderSnippet` cell, and every such call is currently one
			// more instance of the estate-wide Snippet type split (#84) — adding to a known-broken
			// class for a decoration is not worth it. It renders in the review pane instead, which is
			// where someone actually asks "as of when was this the corpus".
			meta: { cellClass: 'font-mono text-xs whitespace-nowrap' },
		},
		{
			id: 'media',
			accessorFn: mediaText,
			header: sortableHeader('media'),
			meta: { cellClass: 'text-xs' },
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
			return visible;
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

	/** The rows the table is CURRENTLY showing — filtered, sorted AND paginated by TanStack.
	 *
	 *  The grid renders these rather than `visible`, so switching mode never silently changes which
	 *  items you are looking at, and a thousand-item queue does not become a thousand tiles. Declared
	 *  here because it reads `table`, which is built just above. */
	const pageRows = $derived(table.getRowModel().rows.map((r) => r.original));

	/** Whether to offer the rest of the filter, and what to say.
	 *
	 *  The header checkbox is TanStack's `toggleAllPageRowsSelected` — it takes the visible page and
	 *  nothing else. Filtering to 347 items and wanting all of them meant ticking 20, acting, paging,
	 *  eighteen times: the filter did the hard part and the selection threw the answer away. */
	const allPrompt = $derived(
		selectAllPrompt({
			pageCount: pageRows.length,
			pageSelectedCount: pageRows.filter((t) => rowSelection[t.task_id]).length,
			selectedCount: selectedIds.length,
			// Every row the FILTER matched, across pages — not the page, and not the project.
			matchingCount: visible.length,
		}),
	);
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

{#snippet labelCell(task: TaskDetail)}
	<!-- The SUGGESTED label, not an accepted one. `secondary` rather than the default variant is the
	     whole point: a prediction must not look like work somebody did. The title names where it came
	     from — the server stamps `source` (`bulk` / `import` / `model:<name>`) and it is the only
	     thing distinguishing a machine's guess from an annotator's answer. -->
	<div class="flex flex-wrap gap-1">
		{#each predictedLabels(task) as label (label)}
			<Badge
				variant="secondary"
				title="suggested{task.prediction?.[0]?.source
					? ` (${task.prediction[0].source})`
					: ''} — not reviewed"
			>
				{label}
			</Badge>
		{/each}
	</div>
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
		<!-- REVIEW beside the queue. First, because on a pre-labelled queue it is the common act:
		     look, judge, next — without losing the filter or the page. -->
		<Button
			variant="ghost"
			size="sm"
			data-testid="row-quick-view"
			title="review this item beside the queue"
			onclick={() => openQuick(task)}
		>
			<Eye class="size-3.5" />
		</Button>
		{#if task.state === 'claimed'}
			<Button
				variant="outline"
				size="sm"
				href={canvasHref(task)}
				title="open this item on the annotate canvas"
			>
				<ExternalLink class="size-3.5" /> Annotate
			</Button>
			<!-- Only on a CLAIMED task, mirroring the server: importing is annotating, so it obeys the
			     same state rule as a draft save rather than being a second way in. -->
			<ImportButton taskId={task.task_id} onimported={onchanged} />
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
						: 'send the item back with a note'}
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

<!-- The queue itself, as a SNIPPET so it can be rendered either alone or as the left half of the
     split. Same instance either way: `{@render}` does not remount, so opening quick view cannot
     reset the table's sort, filter, page or selection — which is the whole reason quick view exists. -->
{#snippet queuePane()}
	<div class="flex flex-col gap-2">
		{#if selectedIds.length > 0}
			<div
				class="bg-muted/50 flex items-center gap-2 rounded-md border px-3 py-1.5 text-sm"
				data-testid="bulk-bar"
			>
				<span class="text-muted-foreground shrink-0">{selectedIds.length} selected</span>

				<!-- Every action the SELECTION can take, derived from the rows' own legal events and in a
			     fixed order — a control that reorders itself between two clicks is how someone accepts a
			     batch they meant to skip. Each names its own count, so a mixed selection is honest about
			     the rows it will not touch instead of acting on a subset silently. -->
				<div class="ml-auto flex flex-wrap items-center justify-end gap-1">
					{#each available as action (action.event)}
						<Button
							variant={action.event === 'accept' ? 'default' : 'outline'}
							size="sm"
							data-testid="bulk-{action.event}"
							disabled={bulkBusy}
							onclick={() => void bulkFire(action.event)}
						>
							{(EVENT_LABELS[action.event] ?? action.event).replace('…', '')}
							{action.count}
						</Button>
					{/each}

					<!-- Stays separate: `assign` needs a RECIPIENT, so it opens a dialog rather than firing. -->
					{#if selectedAssignable.length > 0}
						<Button
							variant="outline"
							size="sm"
							data-testid="bulk-assign"
							disabled={bulkBusy}
							onclick={() => {
								bulkAssignFor = [...selectedAssignable];
								assignee = '';
							}}
						>
							{`Assign ${selectedAssignable.length}`}
						</Button>
					{/if}

					<!-- Removing queued work has no undo, so it confirms — and it is last, away from the
				     actions someone clicks repeatedly. -->
					{#if droppable && selectedIds.length > 0}
						<Button
							variant="ghost"
							size="sm"
							data-testid="bulk-remove"
							disabled={bulkBusy}
							title="remove the selected items from the project"
							onclick={() => void bulkRemove()}
						>
							<Trash2 class="size-3.5" /> Remove {selectedIds.length}
						</Button>
					{/if}
				</div>
			</div>
		{/if}
		<!-- SELECT THE REST. An OFFER, never automatic: silently extending a selection past what someone
	     can see is how a bulk action lands on rows nobody looked at. -->
		{#if allPrompt.kind === 'offer'}
			<p class="text-muted-foreground text-xs" data-testid="select-all-offer">
				All {allPrompt.pageCount} on this page are selected.
				<button
					type="button"
					class="text-foreground underline underline-offset-2"
					data-testid="select-all-matching"
					onclick={() => (rowSelection = selectionOf(visible))}
				>
					Select all {allPrompt.matchingCount.toLocaleString()} matching this filter
				</button>
			</p>
		{:else if allPrompt.kind === 'all'}
			<p class="text-muted-foreground text-xs" data-testid="select-all-held">
				All {allPrompt.matchingCount.toLocaleString()} matching items are selected.
				<button
					type="button"
					class="text-foreground underline underline-offset-2"
					data-testid="select-all-clear"
					onclick={() => (rowSelection = {})}
				>
					Clear selection
				</button>
			</p>
		{/if}

		{#if notice}
			<p class={notice.ok ? 'text-success text-sm' : 'text-destructive text-sm'}>{notice.text}</p>
		{/if}

		<!-- Rendered only when there is something to filter. A filter bar over three items is furniture;
	     over a thousand it is the only way to work. -->
		{#if tasks.length > 1}
			<div class="flex flex-wrap items-center gap-2" data-testid="queue-filter">
				<!-- ONE box across the item's columns — key, corpus, label, modality. A person typing here
			     means "find this anywhere"; asking which column it lives in is asking them to know the
			     schema. -->
				<Input
					bind:value={filterText}
					placeholder="Search items…"
					aria-label="Search items"
					class="h-8 w-56"
					data-testid="filter-text"
					oninput={onFilterChanged}
				/>
				{#if labelsPresent.length > 0}
					<Select
						bind:value={filterLabel}
						ariaLabel="Filter by label"
						onValueChange={onFilterChanged}
						options={[
							{ value: '', label: `All labels (${tasks.length})` },
							...labelsPresent.map((name) => ({
								value: name,
								label: `${name} (${
									tasks.filter((t) =>
										labelText(t)
											.split(',')
											.some((l) => l.trim() === name),
									).length
								})`,
							})),
						]}
					/>
				{/if}
				<Select
					bind:value={filterState}
					ariaLabel="Filter by state"
					onValueChange={onFilterChanged}
					options={[
						{ value: '', label: `All states (${tasks.length})` },
						...statesPresent.map((st) => ({
							value: st,
							// The count is what turns the dropdown into a summary of the queue, so a manager
							// can see WHERE the work is sitting without applying a filter to find out.
							label: `${st} (${tasks.filter((t) => t.state === st).length})`,
						})),
					]}
				/>
				<Input
					bind:value={filterAssignee}
					placeholder="Filter by assignee…"
					aria-label="Filter by assignee"
					class="h-8 w-48"
					oninput={onFilterChanged}
				/>
				<!-- LIST or GRID. Pinned right so it reads as a property of the whole view rather than one
			     more filter. -->
				<div class="ml-auto flex items-center gap-1" data-testid="queue-view-toggle">
					<Button
						variant={view === 'list' ? 'secondary' : 'ghost'}
						size="icon-sm"
						aria-label="List view"
						aria-pressed={view === 'list'}
						data-testid="view-list"
						title="list — audit the queue"
						onclick={() => setView('list')}
					>
						<Rows3 class="size-4" />
					</Button>
					<Button
						variant={view === 'grid' ? 'secondary' : 'ghost'}
						size="icon-sm"
						aria-label="Grid view"
						aria-pressed={view === 'grid'}
						data-testid="view-grid"
						title="grid — scan the images"
						onclick={() => setView('grid')}
					>
						<LayoutGrid class="size-4" />
					</Button>
				</div>

				{#if filtering}
					<span class="text-muted-foreground text-xs" data-testid="filter-count">
						{visible.length} of {tasks.length}
					</span>
					<Button
						variant="ghost"
						size="sm"
						data-testid="clear-filter"
						onclick={() => {
							filterState = '';
							filterAssignee = '';
							filterText = '';
							filterLabel = '';
							onFilterChanged();
						}}
					>
						Clear
					</Button>
				{/if}
			</div>
		{/if}

		{#if view === 'grid' && pageRows.length > 0}
			<!-- The grid renders the TABLE's current page, so filters, sorting and pagination all still
		     apply and the two modes cannot disagree about what is in view. Selection is the same
		     object, so every bulk action above works identically from either. -->
			<TaskGrid
				tasks={pageRows}
				bind:selection={rowSelection}
				apiUrl={(path) => `${base}${path}`}
				onopen={openQuick}
			/>
		{:else}
			<DataTable
				{table}
				emptyMessage={filtering
					? 'No items match this filter.'
					: 'No items yet — send data points in from Search or the Atlas.'}
			/>
		{/if}
	</div>
{/snippet}

{#snippet quickPane()}
	<QuickView
		bind:open={quickOpen}
		bind:taskId={quickId}
		tasks={pageRows}
		apiUrl={(path) => `${base}${path}`}
		{canvasHref}
		busy={busy !== null}
		onfire={(task, event) => void fireFromQuick(task, event)}
	/>
{/snippet}

<!-- Review one item BESIDE the queue — a SPLIT, not an overlay.
     This was a right-side `Sheet`, and the e2e test has been named "reviews an item BESIDE the
     queue" the whole time. An overlay works against the exact pass it exists for: reviewing is a
     SUSTAINED walk with prev/next ("fifty-seven items is fifty-seven round trips"), and a modal
     hides the table so you lose your place, traps focus, and makes changing rows a
     close -> click -> reopen — reintroducing the round trip it removed. Beside the queue, the rows
     stay visible, the next item can be picked straight from the table, and filter/sort stay live.
     Label Studio's Data Manager puts the detail beside the list for the same reason.
     Still NOT the drawing canvas: an item needing shapes goes there, and the pane links to it. -->
{#if quickOpen}
	<!-- Snippets passed INLINE as children, not as `left={queuePane}` props — the shape
	     `AnnotatorShell` already uses for this component. There are two `svelte` installs in the
	     workspace (5.56.3 and 5.56.8), so `Snippet` is not one type across the package boundary and
	     the prop form fails svelte-check with "Two different types with this name exist, but they are
	     unrelated". Inline snippets are declared in THIS module and typed against its own svelte. -->
	<ResizableSplit
		storageKey="annotator-queue-quickview"
		initial={0.62}
		minLeft={420}
		minRight={340}
	>
		{#snippet left()}
			{@render queuePane()}
		{/snippet}
		{#snippet right()}
			{@render quickPane()}
		{/snippet}
	</ResizableSplit>
{:else}
	{@render queuePane()}
{/if}

<!-- BULK assign. A separate dialog from the per-row one rather than a mode flag on it: the two
     differ in what they act on, what they say, and what they do on submit, and a shared dialog
     branching three ways on a nullable pair is how the wrong set gets assigned. -->
<Dialog.Root
	open={bulkAssignFor !== null}
	onOpenChange={(open) => (bulkAssignFor = open ? bulkAssignFor : null)}
>
	<Dialog.Content class="sm:max-w-sm" data-testid="bulk-assign-dialog">
		<Dialog.Title>Assign {bulkAssignFor?.length ?? 0} items</Dialog.Title>
		<Dialog.Description>
			Each is assigned individually and reported individually — there is no transaction across
			items, so a refusal on one leaves the rest alone.
		</Dialog.Description>
		<form
			class="flex flex-col gap-3"
			onsubmit={(e) => {
				e.preventDefault();
				const targets = bulkAssignFor;
				bulkAssignFor = null;
				if (targets && assignee.trim()) void bulkAssign(targets, assignee.trim());
			}}
		>
			<Input bind:value={assignee} placeholder="annotator (OIDC subject or username)" />
			<div class="flex justify-end gap-2">
				<Button type="button" variant="outline" onclick={() => (bulkAssignFor = null)}
					>Cancel</Button
				>
				<Button type="submit" disabled={!assignee.trim()}>Assign all</Button>
			</div>
		</form>
	</Dialog.Content>
</Dialog.Root>

<Dialog.Root
	open={assignFor !== null}
	onOpenChange={(open) => (assignFor = open ? assignFor : null)}
>
	<Dialog.Content class="sm:max-w-sm">
		<Dialog.Title>Assign this item</Dialog.Title>
		<Dialog.Description>
			The named annotator holds it with a pinned lease (it never expires) until they submit,
			release, or a manager reassigns.
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
