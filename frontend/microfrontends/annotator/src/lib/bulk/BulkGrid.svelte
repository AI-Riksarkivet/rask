<script lang="ts">
	// OPEN-BULK phases 1+2 — the labeling task as a GRID.
	//
	// Rows are the task's ITEMS; columns are what a reviewer scans for: the media, the item's
	// corpus facts, its workflow state, and its LIVE annotation state (status counts, item
	// tags, a transcription excerpt). The annotation columns are fetched LAZILY, per row, when
	// the row scrolls into view — a task holds up to SEND_TASK_CAP (1000) items, and reading a
	// thousand Arrow tables up front to render twenty rows would be the bulk-spec §3.5 mistake
	// this surface explicitly refuses. Row height is fixed and off-screen rows skip layout
	// (`content-visibility`), which carries a 1000-row grid without a virtualization library;
	// real windowing is later work if measured to matter.
	//
	// Phase 2 makes two cells ACT, through the same save wire the canvas uses (per-field edits +
	// base_version OCC — never a second store): ✓ flips every `prediction` row to `accepted` in
	// one atomic save, and ✎ edits the transcription excerpt in place. The server stamps
	// `reviewer`/`updated_at` on every touched row; the grid re-fetches and renders that
	// attribution rather than claiming it client-side. A 409 (someone else saved first) is
	// resolved by re-fetching — the refreshed row IS the answer for a status flip; an edit
	// collision keeps the draft open over the fresh state so nothing typed is lost silently.
	import { onDestroy } from 'svelte';
	import { base } from '$app/paths';
	import {
		loadAnnotations,
		makeInsertRow,
		postSave,
		type SavePayload,
	} from '@rask/labeling/annotations-client';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import * as Popover from '@rask/ui/popover';
	import { Textarea } from '@rask/ui/textarea';
	import MediaThumb from '$lib/viewer/layout/MediaThumb.svelte';
	import { statusVariant } from '$lib/viewer/layout/statusStyle';
	import { taskCanvasHref } from '$lib/viewer/task-stream';
	import { fetchCorpusRows } from '$lib/projects/remote/rows.remote';
	import { updateProjectOntology } from '$lib/projects/remote/projects.remote';
	import { assistProducers, requestAssist } from '$lib/viewer/remote/assist.remote';
	import { indexRows, rowFor, rowKeysFor, rowText } from '$lib/projects/corpus-rows';
	import type { TaskDetail } from '$lib/projects/types';
	import { type AnnotationSummary, summarize } from './summary.js';
	import { deriveColumnName, recipeColumns, withRecipeClass, type Ontology } from './recipe.js';
	import { SvelteMap, SvelteSet } from 'svelte/reactivity';

	let {
		projectId,
		tasks,
		ontology,
	}: {
		projectId: string;
		tasks: TaskDetail[];
		/** The task's current ontology — recipe columns project from it. Absent = none render. */
		ontology?: Ontology;
	} = $props();

	/** How many rows an act-first column fills IMMEDIATELY — judge the prompt cheaply before
	 *  running thousands (the preview-5 economics; scoped/whole-task runs are the jobs seam). */
	const PREVIEW_ROWS = 5;

	// Corpus facets are DECORATION on a grid that works without them (the queue's own rule):
	// `?? []` everywhere, never a loading gate.
	const rowsQuery = $derived(fetchCorpusRows({ keys: rowKeysFor(tasks), dataset: null }));
	const rowIndex = $derived(
		indexRows(rowsQuery.current?.rows ?? [], rowsQuery.current?.keyFields ?? []),
	);
	const rowKeyFields = $derived(rowsQuery.current?.keyFields ?? []);

	// The lazy annotation-state cache, keyed by task id. The VERSION rides along because it is
	// the OCC handshake a save must echo. `null` = fetch failed (rendered as an honest "—",
	// never retried in a loop); absent = not yet visible.
	type ItemState = { summary: AnnotationSummary; version: number };
	const items = new SvelteMap<string, ItemState | null>();
	const inflight = new Set<string>();
	/** Task ids with a save in flight — their action buttons disable, nothing else blocks. */
	const saving = new SvelteSet<string>();

	// The single open inline edit (one at a time is the spreadsheet convention): the draft the
	// textarea binds to, kept OUTSIDE the cache so a background refresh cannot eat a keystroke.
	let editing = $state<{ taskId: string; draft: string } | null>(null);

	// ── act-first "＋ column" (phase 3) ───────────────────────────────────────────────────────
	// One textarea; Enter DERIVES the declaration (name from the action, tag+transcribe class),
	// PATCHes the ontology silently, and fills the first rows through the assist wire. The
	// column list unions the prop's ontology with names added THIS session, so the new column
	// renders before the project re-fetch lands.
	let columnAction = $state('');
	let columnOpen = $state(false);
	let addingColumn = $state(false);
	let selectedProducer = $state('');
	const localColumns = new SvelteSet<string>();
	/** Cells with a fill in flight, keyed `taskId:column` — rendered as a streaming cell. */
	const filling = new SvelteSet<string>();

	const producersQuery = $derived(assistProducers(null));
	// Recipe producers: interactive families whose declared returns include the `tag` an
	// item-level answer lands as. The picker lists what discovery/config actually offers —
	// users never type an endpoint (open_assist_discovery.md §"Who configures what").
	const recipeProducers = $derived.by(() => {
		const listing = producersQuery.current;
		if (!listing?.ok) return [];
		return listing.data.producers.filter(
			(p) => p.interactive !== false && p.returns.includes('tag'),
		);
	});
	$effect(() => {
		if (!selectedProducer) selectedProducer = recipeProducers[0]?.name ?? '';
	});
	const columns = $derived([...new Set([...recipeColumns(ontology), ...localColumns])]);
	// Recipe labels render as their own columns — keep them out of the Tags chips.
	const chipTags = (summary: AnnotationSummary): string[] =>
		summary.tags.filter((t) => !columns.includes(t));

	async function addColumn(): Promise<void> {
		const action = columnAction.trim();
		if (!action || !ontology || !selectedProducer || addingColumn) return;
		addingColumn = true;
		try {
			const name = deriveColumnName(action, [...ontology.classes.map((c) => c.name), ...columns]);
			// The command single-flights the project re-fetch itself; `localColumns` renders the
			// column before that read lands.
			const result = await updateProjectOntology({
				projectId,
				ontology: withRecipeClass(ontology, name),
			});
			if (!result.ok) return;
			localColumns.add(name);
			columnOpen = false;
			columnAction = '';
			// Preview-first: fill the FIRST rows immediately so the prompt is judged cheaply;
			// sequential on purpose (one cell visibly lands after another, and the mock/model
			// is not hammered with a burst).
			for (const task of tasks.slice(0, PREVIEW_ROWS)) {
				await fillCell(task, name, action);
			}
		} finally {
			addingColumn = false;
		}
	}

	/** One cell fill: ask the producer, land the answer as a `tag` row with `status='prediction'`
	 *  through the ordinary save wire. The cell's provenance is the row's `source`; a re-fetch
	 *  renders whatever the server committed. */
	async function fillCell(task: TaskDetail, column: string, prompt: string): Promise<void> {
		const url = annotationsUrl(task);
		const cellKey = `${task.task_id}:${column}`;
		if (!url || filling.has(cellKey)) return;
		filling.add(cellKey);
		try {
			// The save needs the row's current version — make sure it is loaded.
			if (!items.has(task.task_id)) await fetchSummary(task);
			const state = items.get(task.task_id);
			if (!state) return;
			// `taskId: null` DELIBERATELY: each item captured its ontology at send, so a column
			// appended NOW is absent from that capture by definition — passing the task id would
			// have the contract filter drop the very answers this fill exists to produce. The
			// membership rules still apply where they mean something (submit). Capture-refresh
			// semantics are phase 3b's open question (open_bulk_active.md §6.3).
			const result = await requestAssist({
				key: (task.source.keys ?? []).join(','),
				dataset: task.source.where ?? null,
				producer: selectedProducer,
				prompt,
				taskId: null,
				region: null,
				points: null,
			});
			if (!result.ok) return;
			const answer = result.data.shapes.find((s) => (s.text ?? '') !== '');
			if (!answer) return;
			const row = makeInsertRow({
				shape_type: 'tag',
				label: column,
				text: answer.text ?? '',
				status: 'prediction',
				source: result.data.source,
				confidence: answer.confidence ?? null,
				uncertainty: answer.uncertainty ?? null,
			});
			await postSave(url, {
				edits: [],
				inserts: [row],
				geometry: [],
				temporal: [],
				spans: [],
				deletes: [],
				base_version: state.version,
			});
			await fetchSummary(task, { force: true });
		} catch {
			// A failed cell stays empty — visibly unfilled, retryable by re-running the column.
		} finally {
			filling.delete(cellKey);
		}
	}

	/** Accept ONE recipe cell — the same status flip as the row-level accept, scoped to a cell. */
	async function acceptCell(task: TaskDetail, cell: { id: string }): Promise<void> {
		await saveEdits(task, [{ id: cell.id, status: 'accepted' }]);
	}

	function annotationsUrl(task: TaskDetail): string | null {
		const key = (task.source.keys ?? []).join(',');
		if (!key) return null;
		const ds = task.source.where ? `?dataset=${encodeURIComponent(task.source.where)}` : '';
		return `${base}/api/annotations/${key}${ds}`;
	}

	async function fetchSummary(task: TaskDetail, { force = false } = {}): Promise<void> {
		const url = annotationsUrl(task);
		if (!url || inflight.has(task.task_id)) return;
		if (!force && items.has(task.task_id)) return;
		inflight.add(task.task_id);
		try {
			const { table, version } = await loadAnnotations(url);
			items.set(task.task_id, { summary: summarize(table), version });
		} catch {
			items.set(task.task_id, null);
		} finally {
			inflight.delete(task.task_id);
		}
	}

	/** One save of per-field edits against the version this row was rendered from. Ok or
	 *  conflict, the row re-fetches — the wire's fresh state is the single source of truth. */
	async function saveEdits(
		task: TaskDetail,
		edits: SavePayload['edits'],
	): Promise<'ok' | 'conflict' | 'error'> {
		const url = annotationsUrl(task);
		const state = items.get(task.task_id);
		if (!url || !state || saving.has(task.task_id)) return 'error';
		saving.add(task.task_id);
		try {
			const payload: SavePayload = {
				edits,
				inserts: [],
				geometry: [],
				temporal: [],
				spans: [],
				deletes: [],
				base_version: state.version,
			};
			const { status } = await postSave(url, payload);
			await fetchSummary(task, { force: true });
			return status;
		} catch {
			return 'error';
		} finally {
			saving.delete(task.task_id);
		}
	}

	async function acceptPredictions(task: TaskDetail): Promise<void> {
		const state = items.get(task.task_id);
		if (!state || state.summary.predictionIds.length === 0) return;
		await saveEdits(
			task,
			state.summary.predictionIds.map((id) => ({ id, status: 'accepted' })),
		);
	}

	function openEdit(task: TaskDetail): void {
		const state = items.get(task.task_id);
		if (!state?.summary.textId) return;
		editing = { taskId: task.task_id, draft: state.summary.text };
	}

	async function commitEdit(task: TaskDetail): Promise<void> {
		const state = items.get(task.task_id);
		if (!editing || editing.taskId !== task.task_id || !state?.summary.textId) return;
		const outcome = await saveEdits(task, [{ id: state.summary.textId, text: editing.draft }]);
		// A conflict keeps the draft open over the re-fetched row — the reviewer decides what
		// their words are worth against the newer state; only a clean save closes the editor.
		if (outcome === 'ok') editing = null;
	}

	/** Fetch-on-visibility: one shared observer, rows register through a `use:` action. */
	const byElement = new WeakMap<Element, TaskDetail>();
	const observer =
		typeof IntersectionObserver === 'undefined'
			? null
			: new IntersectionObserver(
					(entries) => {
						for (const entry of entries) {
							const task = byElement.get(entry.target);
							if (entry.isIntersecting && task) void fetchSummary(task);
						}
					},
					{ rootMargin: '200px 0px' },
				);
	function visible(node: Element, task: TaskDetail) {
		byElement.set(node, task);
		observer?.observe(node);
		return {
			destroy() {
				observer?.unobserve(node);
			},
		};
	}
	onDestroy(() => observer?.disconnect());

	const mediaKind = (task: TaskDetail): 'image' | 'audio' | 'video' | 'text' => {
		const kind = task.media?.kind;
		return kind === 'audio' || kind === 'video' || kind === 'text' ? kind : 'image';
	};
	const thumbSrc = (task: TaskDetail): string | null => {
		const key = (task.source.keys ?? []).join(',');
		if (!key || mediaKind(task) !== 'image') return null;
		const ds = task.source.where ? `?dataset=${encodeURIComponent(task.source.where)}` : '';
		return `${base}/api/chunk-frame/${key}${ds}`;
	};
	const reviewedTitle = (summary: AnnotationSummary): string =>
		summary.reviewedAt === null
			? `last touched by ${summary.reviewer}`
			: `last touched by ${summary.reviewer} · ${new Date(summary.reviewedAt).toLocaleString()}`;
</script>

<div class="border-border overflow-auto rounded-md border" data-testid="bulk-grid">
	<table class="w-full text-left text-xs">
		<thead class="bg-muted/60 text-muted-foreground sticky top-0 z-10">
			<tr>
				<th class="w-20 px-2 py-1.5 font-medium">Item</th>
				<th class="px-2 py-1.5 font-medium">Key</th>
				<th class="px-2 py-1.5 font-medium">State</th>
				<th class="px-2 py-1.5 font-medium">Assignee</th>
				<th class="px-2 py-1.5 font-medium">Corpus</th>
				<th class="px-2 py-1.5 font-medium">Regions</th>
				<th class="px-2 py-1.5 font-medium">Tags</th>
				<th class="px-2 py-1.5 font-medium">Text</th>
				{#each columns as column (column)}
					<th class="px-2 py-1.5 font-medium" data-testid="bulk-column-{column}">{column}</th>
				{/each}
				<th class="px-2 py-1.5 font-normal">
					<Popover.Root bind:open={columnOpen}>
						<Popover.Trigger>
							{#snippet child({ props })}
								<Button
									{...props}
									variant="ghost"
									size="sm"
									class="h-6 whitespace-nowrap px-1.5 text-[11px]"
									data-testid="bulk-add-column"
									disabled={!ontology}
								>
									＋ column
								</Button>
							{/snippet}
						</Popover.Trigger>
						<Popover.Content class="w-96 p-3" align="end">
							<div class="flex flex-col gap-2">
								<p class="text-muted-foreground text-xs">
									Type your action — the column is named from it, appended to the task's ontology, and the
									first {PREVIEW_ROWS} rows fill immediately.
								</p>
								<Textarea
									class="min-h-16 text-xs"
									placeholder="e.g. which century is this document from?"
									bind:value={columnAction}
									data-testid="bulk-column-action"
									onkeydown={(event: KeyboardEvent) => {
	if (event.key === 'Enter' && !event.shiftKey) {
		event.preventDefault();
		void addColumn();
	}
}}
								/>
								<label class="text-muted-foreground flex items-center gap-2 text-xs">
									model
									<select
										class="border-input bg-background h-6 min-w-0 flex-1 rounded border px-1 text-xs"
										bind:value={selectedProducer}
										data-testid="bulk-column-producer"
									>
										{#each recipeProducers as producer (producer.name)}
											<option value={producer.name}>
												{producer.name}{producer.configured ? '' : ' (mock)'}
											</option>
										{/each}
									</select>
								</label>
								<div class="flex justify-end">
									<Button
										size="sm"
										class="h-6 px-2 text-xs"
										disabled={addingColumn || !columnAction.trim() || !selectedProducer}
										data-testid="bulk-column-go"
										onclick={() => addColumn()}
									>
										{addingColumn ? 'adding…' : '⏎ go'}
									</Button>
								</div>
							</div>
						</Popover.Content>
					</Popover.Root>
				</th>
			</tr>
		</thead>
		<tbody>
			{#each tasks as task (task.task_id)}
				{@const state = items.get(task.task_id)}
				{@const summary = state === undefined ? undefined : (state?.summary ?? null)}
				<tr
					class="border-border/60 hover:bg-accent/40 border-t align-middle [content-visibility:auto] [contain-intrinsic-block-size:auto_56px]"
					data-testid="bulk-row"
					use:visible={task}
				>
					<td class="px-2 py-1">
						<a href={taskCanvasHref(task, projectId, base)} data-sveltekit-preload-data="off">
							<MediaThumb
								src={thumbSrc(task)}
								kind={mediaKind(task)}
								alt={task.source.keys?.join(',') ?? ''}
								ratio="aspect-[4/3]"
							/>
						</a>
					</td>
					<td class="max-w-40 truncate px-2 py-1 font-mono text-[11px]">
						<a
							class="hover:underline"
							href={taskCanvasHref(task, projectId, base)}
							data-sveltekit-preload-data="off"
						>
							{task.source.keys?.join(',')}
						</a>
					</td>
					<td class="px-2 py-1">
						<Badge variant={statusVariant(task.state)}>{task.state}</Badge>
					</td>
					<td class="text-muted-foreground max-w-28 truncate px-2 py-1">{task.assignee ?? '—'}</td>
					<td class="text-muted-foreground max-w-56 truncate px-2 py-1">
						{rowText(rowFor(task, rowIndex), rowKeyFields) || '—'}
					</td>
					<td class="px-2 py-1" data-testid="bulk-regions">
						{#if summary === undefined}
							<span class="text-muted-foreground">…</span>
						{:else if summary === null}
							<span class="text-muted-foreground" title="annotations unreadable">—</span>
						{:else}
							<span class="flex flex-wrap items-center gap-1">
								{#each Object.entries(summary.byStatus) as [status, count] (status)}
									<Badge variant={statusVariant(status)} class="text-[10px]">
										{status}
										{count}
									</Badge>
								{/each}
								{#if summary.total === 0}
									<span class="text-muted-foreground">empty</span>
								{/if}
								{#if summary.predictionIds.length > 0}
									<Button
										variant="outline"
										size="sm"
										class="h-5 px-1.5 text-[10px]"
										disabled={saving.has(task.task_id)}
										title="Accept all {summary.predictionIds
											.length} predicted annotations on this item"
										data-testid="bulk-accept"
										onclick={() => acceptPredictions(task)}
									>
										✓ accept {summary.predictionIds.length}
									</Button>
								{/if}
								{#if summary.reviewer}
									<span
										class="text-muted-foreground text-[10px]"
										title={reviewedTitle(summary)}
										data-testid="bulk-reviewed"
									>
										✓ {summary.reviewer}
									</span>
								{/if}
							</span>
						{/if}
					</td>
					<td class="px-2 py-1" data-testid="bulk-tags">
						{#if summary}
							<span class="flex flex-wrap gap-1">
								{#each chipTags(summary) as tag (tag)}
									<Badge variant="secondary" class="text-[10px]">{tag}</Badge>
								{/each}
							</span>
						{/if}
					</td>
					<td class="max-w-64 px-2 py-1 text-[11px]" data-testid="bulk-text">
						{#if editing !== null && editing.taskId === task.task_id}
							<span class="flex flex-col gap-1">
								<Textarea
									class="min-h-14 text-[11px]"
									bind:value={editing.draft}
									data-testid="bulk-text-editor"
									onkeydown={(event: KeyboardEvent) => {
	if (event.key === 'Escape') editing = null;
	if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
		event.preventDefault();
		void commitEdit(task);
	}
}}
								/>
								<span class="flex gap-1">
									<Button
										size="sm"
										class="h-5 px-1.5 text-[10px]"
										disabled={saving.has(task.task_id)}
										data-testid="bulk-text-save"
										onclick={() => commitEdit(task)}
									>
										Save
									</Button>
									<Button
										variant="ghost"
										size="sm"
										class="h-5 px-1.5 text-[10px]"
										onclick={() => (editing = null)}
									>
										Cancel
									</Button>
								</span>
							</span>
						{:else}
							<span class="flex items-center gap-1">
								<span class="text-muted-foreground truncate">{summary?.text || ''}</span>
								{#if summary?.textId}
									<Button
										variant="ghost"
										size="sm"
										class="h-5 shrink-0 px-1 text-[10px]"
										title="Edit this transcription"
										data-testid="bulk-text-edit"
										onclick={() => openEdit(task)}
									>
										✎
									</Button>
								{/if}
							</span>
						{/if}
					</td>
					{#each columns as column (column)}
						{@const cell = summary?.tagCells[column]}
						<td class="max-w-56 px-2 py-1 text-[11px]" data-testid="bulk-cell-{column}">
							{#if filling.has(`${task.task_id}:${column}`)}
								<span class="text-muted-foreground animate-pulse">▍generating…</span>
							{:else if cell}
								<span class="flex items-center gap-1">
									<span
										class="truncate {cell.status === 'prediction'
											? 'text-primary'
											: 'text-foreground'}"
										title={cell.text}
									>
										{cell.text || cell.status}
									</span>
									{#if cell.status === 'prediction'}
										<Button
											variant="ghost"
											size="sm"
											class="h-5 shrink-0 px-1 text-[10px]"
											disabled={saving.has(task.task_id)}
											title="Accept this cell"
											data-testid="bulk-cell-accept"
											onclick={() => acceptCell(task, cell)}
										>
											✓
										</Button>
									{/if}
								</span>
							{:else}
								<span class="text-muted-foreground">—</span>
							{/if}
						</td>
					{/each}
					<td></td>
				</tr>
			{/each}
		</tbody>
	</table>
	{#if tasks.length === 0}
		<p class="text-muted-foreground p-4 text-sm">No items in this labeling task yet.</p>
	{/if}
</div>
