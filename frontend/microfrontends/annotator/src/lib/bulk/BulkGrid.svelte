<script lang="ts">
	// OPEN-BULK — the labeling task as a SPREADSHEET.
	//
	// Bulk labeling is a special case of labeling, done in bulk: the SAME task, the SAME
	// ontology — only the modality changes, a table over all the session's items instead of a
	// canvas over one. Two consequences shape everything here:
	//
	// * Items arrive ALREADY CLAIMED into the bulk session. Claiming is a precondition of
	//   being in this grid, never something done here — so there is no State/Assignee/queue
	//   chrome. The grid shows LABELING state (statuses, cells, attribution), not dispatch.
	// * Columns are the ontology, and the ontology is fluid: each recipe column is a
	//   declaration appended act-first (one textarea, the name derived from the action, a
	//   silent ontology PATCH), fillable by a discovered model, filterable like a spreadsheet.
	//
	// Annotation state is fetched LAZILY per visible row (IntersectionObserver +
	// `content-visibility` rows — a task holds up to 1000 items and reading a thousand Arrow
	// tables to render twenty rows is the mistake the spec refuses); activating a
	// content filter eagerly loads the rest, because a filter over unread rows would silently
	// filter nothing. All writes ride the canvas's save wire (per-field edits + base_version
	// OCC — never a second store); the server stamps reviewer/updated_at and the grid renders
	// the stamp, never claiming identity client-side.
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
	import { updateProjectOntology } from '$lib/projects/remote/projects.remote';
	import { assistProducers, requestAssist } from '$lib/viewer/remote/assist.remote';
	import { fetchCorpusRows } from '$lib/projects/remote/rows.remote';
	import { rowKeysFor } from '$lib/projects/corpus-rows';
	import { findSimilar } from '$lib/select/remote/similar.remote';
	import { MAX_SIMILAR_N } from '$lib/select/similar-limits';
	import { distanceBounds } from '$lib/select/similar';
	import type { TaskDetail } from '$lib/projects/types';
	import { type AnnotationSummary, summarize } from './summary.js';
	import { deriveColumnName, recipeColumns, withRecipeClass, type Ontology } from './recipe.js';
	import {
		distanceOf,
		inNeighborhood,
		neighborhoodOf,
		orderedByDistance,
		taskKeyOf,
		type Neighborhood,
	} from './similarity.js';
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
	 *  running many (the preview-5 economics; whole-task runs are the jobs seam). */
	const PREVIEW_ROWS = 5;
	/** How many empty cells one ▶ apply fills — bounded like the drag-fill it stands in for. */
	const FILL_BATCH = 25;

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

	// ── act-first "＋ column" ─────────────────────────────────────────────────────────────────
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
	/** Each column's recipe as authored THIS session — what ▶ re-applies to empty cells.
	 *  Persisting the recipe with the task (so it survives a reload and travels to the next
	 *  annotator) is phase 3b (spec §6.3). */
	const recipes = new SvelteMap<string, { producer: string; prompt: string }>();

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

	// ── per-column filters (the spreadsheet's autofilter) ────────────────────────────────────
	// Contains-match per column, case-insensitive. A row whose summary is still unread passes
	// every content filter (it filters IN once read); setting any content filter eagerly loads
	// the remaining summaries, because filtering rows nobody has read would silently show a
	// subset and call it the result. Embedding-similarity filters ride the same row-predicate
	// seam later (spec §5 — the reason bulk sees all items at once).
	const filters = new SvelteMap<string, string>();
	const setFilter = (column: string, value: string) => {
		if (value.trim()) filters.set(column, value);
		else filters.delete(column);
	};

	// ── the EMBEDDING view: anchor a row, work its neighborhood ──────────────────────────────
	// ≈ on a row asks the estate's ONE similarity seam (`/api/search/similar`, the same wire the
	// select surface uses) for its nearest items; the grid re-orders nearest-first, shows each
	// row's distance, and a cutoff slider narrows the set. Every set-level action — the ▶
	// column apply, accepts, the autofilters — already operates on the FILTERED rows, so
	// "label the neighborhood of this page" is: anchor → tighten → apply. The corpus read
	// below exists to map hits back onto rows by the declared identity fields.
	const rowsQuery = $derived(fetchCorpusRows({ keys: rowKeysFor(tasks), dataset: null }));
	const keyFields = $derived(rowsQuery.current?.keyFields ?? []);
	let anchor = $state<{ taskId: string; key: string } | null>(null);
	/** The raw hits — the association into a Neighborhood is DERIVED, because `keyFields`
	 *  arrives from its own read and a hood built against a not-yet-landed identity would
	 *  silently associate nothing and render an unranked grid that looks like an answer. */
	let hits = $state<Record<string, unknown>[] | null>(null);
	let similarError = $state('');
	/** null = untouched: the slider starts at the furthest returned distance (wide open — the
	 *  cutoff tightens by LOOKING, select-surface rule); a drag overrides. */
	let cutoffOverride = $state<number | null>(null);
	const hood = $derived.by((): Neighborhood | null => {
		if (hits === null || keyFields.length === 0) return null;
		return neighborhoodOf(hits, keyFields);
	});
	const bounds = $derived(hits === null ? null : distanceBounds(hits));
	const cutoff = $derived(cutoffOverride ?? bounds?.furthest ?? 1);
	const similarNote = $derived.by(() => {
		if (similarError) return similarError;
		if (hits !== null && keyFields.length === 0 && (rowsQuery.current?.status ?? 0) !== 200)
			return 'row identity is unavailable — neighbours cannot be matched onto rows';
		if (hood !== null && !hood.hasDistances)
			return 'this corpus declares no vector space — rows are unranked';
		return '';
	});

	async function anchorSimilar(task: TaskDetail): Promise<void> {
		anchor = { taskId: task.task_id, key: taskKeyOf(task) };
		hits = null;
		similarError = '';
		cutoffOverride = null;
		const result = await findSimilar({
			key: taskKeyOf(task),
			dataset: task.source.where ?? null,
			n: MAX_SIMILAR_N,
		});
		if (!result.ok) {
			// The service's own refusal words are the actionable part ("this corpus declares no
			// vector space", "the search service is unreachable") — shown, never swallowed.
			similarError = result.detail || 'the search service is unreachable';
			return;
		}
		hits = result.data;
	}
	function clearSimilar(): void {
		anchor = null;
		hits = null;
		similarError = '';
		cutoffOverride = null;
	}
	const contentFilterActive = $derived([...filters.keys()].some((k) => k !== 'key'));
	function passes(task: TaskDetail): boolean {
		const summary = items.get(task.task_id)?.summary;
		for (const [column, raw] of filters) {
			const needle = raw.trim().toLowerCase();
			if (!needle) continue;
			if (column === 'key') {
				if (!(task.source.keys ?? []).join(',').toLowerCase().includes(needle)) return false;
				continue;
			}
			if (!summary) continue; // unread rows stay visible until their read lands
			const haystack =
				column === 'tags'
					? chipTags(summary).join(' ')
					: column === 'text'
						? summary.text
						: (summary.tagCells[column]?.text ?? '');
			if (!haystack.toLowerCase().includes(needle)) return false;
		}
		return true;
	}
	const visibleTasks = $derived.by(() => {
		const kept = tasks.filter(
			(task) => passes(task) && (hood === null || inNeighborhood(task, hood, cutoff)),
		);
		return hood === null ? kept : orderedByDistance(kept, hood);
	});
	/** How many rows the neighborhood holds BEFORE the cutoff — the slider's honest label. */
	const neighborhoodSize = $derived(
		hood === null
			? 0
			: tasks.filter((t) => hood !== null && inNeighborhood(t, hood, Number.POSITIVE_INFINITY))
					.length,
	);
	let loadingAll = false;
	$effect(() => {
		if (contentFilterActive) void loadAllSummaries();
	});
	async function loadAllSummaries(): Promise<void> {
		if (loadingAll) return;
		loadingAll = true;
		try {
			const missing = tasks.filter((t) => !items.has(t.task_id));
			for (let i = 0; i < missing.length; i += 6) {
				await Promise.all(missing.slice(i, i + 6).map((t) => fetchSummary(t)));
			}
		} finally {
			loadingAll = false;
		}
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
			recipes.set(name, { producer: selectedProducer, prompt: action });
			columnOpen = false;
			columnAction = '';
			// Preview-first: fill the FIRST rows immediately so the prompt is judged cheaply;
			// sequential on purpose (one cell visibly lands after another, and the mock/model
			// is not hammered with a burst).
			for (const task of tasks.slice(0, PREVIEW_ROWS)) {
				await fillCell(task, name, action, selectedProducer);
			}
		} finally {
			addingColumn = false;
		}
	}

	/** The column-level APPLY (▶): run the column's recipe over the next empty cells among the
	 *  currently FILTERED rows — filters scope the run, exactly the excel-grade move the spec
	 *  steals ("run on this filtered slice" before "run on all N"). Filled cells are skipped;
	 *  validated cells are never touched by construction (a fill INSERTS, an accept EDITS). */
	async function fillEmptyCells(column: string): Promise<void> {
		const recipe = recipes.get(column);
		if (!recipe) return;
		const targets = visibleTasks
			.filter((t) => !items.get(t.task_id)?.summary.tagCells[column])
			.slice(0, FILL_BATCH);
		for (const task of targets) {
			await fillCell(task, column, recipe.prompt, recipe.producer);
		}
	}

	/** One cell fill: ask the producer, land the answer as a `tag` row with `status='prediction'`
	 *  through the ordinary save wire. The cell's provenance is the row's `source`; a re-fetch
	 *  renders whatever the server committed. */
	async function fillCell(
		task: TaskDetail,
		column: string,
		prompt: string,
		producer: string,
	): Promise<void> {
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
				producer,
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
			// A failed cell stays empty — visibly unfilled, retryable by re-applying the column.
		} finally {
			filling.delete(cellKey);
		}
	}

	/** Accept ONE recipe cell — the same status flip as the row-level accept, scoped to a cell. */
	async function acceptCell(task: TaskDetail, cell: { id: string }): Promise<void> {
		await saveEdits(task, [{ id: cell.id, status: 'accepted' }]);
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
	{#if anchor}
		<!-- The similarity rail: what is anchored, how tight the neighborhood is, and the honest
		     count — every set-level action below operates on exactly these rows. -->
		<div
			class="bg-muted/40 border-border/60 flex flex-wrap items-center gap-3 border-b px-3 py-1.5 text-xs"
			data-testid="bulk-similarity-bar"
		>
			<span class="font-medium">≈ similar to <span class="font-mono">{anchor.key}</span></span>
			{#if hood === null && !similarNote}
				<span class="text-muted-foreground">searching…</span>
			{:else if hood?.hasDistances}
				<label class="text-muted-foreground flex items-center gap-2">
					cutoff
					<input
						type="range"
						min={bounds?.nearest ?? 0}
						max={bounds?.furthest ?? 1}
						step="any"
						value={cutoff}
						oninput={(e) => (cutoffOverride = Number(e.currentTarget.value))}
						data-testid="bulk-similarity-cutoff"
					/>
					<span class="font-mono">{cutoff.toFixed(3)}</span>
				</label>
				<span class="text-muted-foreground" data-testid="bulk-similarity-count">
					{visibleTasks.length} of {neighborhoodSize} neighbours in view
				</span>
			{/if}
			{#if similarNote}
				<span class="text-warning">{similarNote}</span>
			{/if}
			<Button
				variant="ghost"
				size="sm"
				class="h-5 px-1.5 text-[10px]"
				data-testid="bulk-similarity-clear"
				onclick={clearSimilar}
			>
				✕ clear
			</Button>
		</div>
	{/if}
	<table class="w-full text-left text-xs">
		<thead class="bg-muted/60 text-muted-foreground sticky top-0 z-10">
			<tr>
				<th class="w-20 px-2 py-1.5 font-medium">Item</th>
				<th class="px-2 py-1.5 font-medium">Key</th>
				<th class="px-2 py-1.5 font-medium">Labels</th>
				<th class="px-2 py-1.5 font-medium">Tags</th>
				<th class="px-2 py-1.5 font-medium">Text</th>
				{#each columns as column (column)}
					<th class="px-2 py-1.5 font-medium" data-testid="bulk-column-{column}">
						<span class="flex items-center gap-1">
							{column}
							{#if recipes.has(column)}
								<Button
									variant="ghost"
									size="sm"
									class="h-5 shrink-0 px-1 text-[10px]"
									title="Apply this column's recipe to the next {FILL_BATCH} empty cells (filtered rows only)"
									data-testid="bulk-fill-{column}"
									onclick={() => fillEmptyCells(column)}
								>
									▶
								</Button>
							{/if}
						</span>
					</th>
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
			<!-- The autofilter row: contains-match per column; content filters eager-load the rest. -->
			<tr class="border-border/60 border-t">
				<th class="px-2 py-1"></th>
				<th class="px-2 py-1">
					<input
						class="border-input bg-background h-5 w-full rounded border px-1 font-normal"
						placeholder="filter…"
						data-testid="bulk-filter-key"
						oninput={(e) => setFilter('key', e.currentTarget.value)}
					/>
				</th>
				<th class="px-2 py-1"></th>
				<th class="px-2 py-1">
					<input
						class="border-input bg-background h-5 w-full rounded border px-1 font-normal"
						placeholder="filter…"
						data-testid="bulk-filter-tags"
						oninput={(e) => setFilter('tags', e.currentTarget.value)}
					/>
				</th>
				<th class="px-2 py-1">
					<input
						class="border-input bg-background h-5 w-full rounded border px-1 font-normal"
						placeholder="filter…"
						data-testid="bulk-filter-text"
						oninput={(e) => setFilter('text', e.currentTarget.value)}
					/>
				</th>
				{#each columns as column (column)}
					<th class="px-2 py-1">
						<input
							class="border-input bg-background h-5 w-full rounded border px-1 font-normal"
							placeholder="filter…"
							data-testid="bulk-filter-{column}"
							oninput={(e) => setFilter(column, e.currentTarget.value)}
						/>
					</th>
				{/each}
				<th class="px-2 py-1"></th>
			</tr>
		</thead>
		<tbody>
			{#each visibleTasks as task (task.task_id)}
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
					<td class="max-w-48 px-2 py-1 font-mono text-[11px]">
						<span class="flex items-center gap-1">
							<a
								class="truncate hover:underline"
								href={taskCanvasHref(task, projectId, base)}
								data-sveltekit-preload-data="off"
							>
								{task.source.keys?.join(',')}
							</a>
							{#if hood !== null && distanceOf(task, hood) !== undefined}
								<span class="text-muted-foreground shrink-0" data-testid="bulk-distance">
									{distanceOf(task, hood)?.toFixed(3)}
								</span>
							{/if}
							<Button
								variant="ghost"
								size="sm"
								class="h-5 shrink-0 px-1 text-[10px] {anchor?.taskId === task.task_id
									? 'text-primary'
									: ''}"
								title="Find items similar to this one (embedding neighbours)"
								data-testid="bulk-similar"
								onclick={() => anchorSimilar(task)}
							>
								≈
							</Button>
						</span>
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
	{:else if visibleTasks.length === 0}
		<p class="text-muted-foreground p-4 text-sm" data-testid="bulk-no-match">
			No items match the filters.
		</p>
	{/if}
</div>
