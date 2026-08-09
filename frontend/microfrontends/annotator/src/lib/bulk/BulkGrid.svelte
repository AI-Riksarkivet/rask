<script lang="ts">
	// OPEN-BULK phase 1 — the labeling task as a GRID (read-only).
	//
	// Rows are the task's ITEMS; columns are what a reviewer scans for: the media, the item's
	// corpus facts, its workflow state, and its LIVE annotation state (status counts, item
	// tags, a transcription excerpt). The annotation columns are fetched LAZILY, per row, when
	// the row scrolls into view — a task holds up to SEND_TASK_CAP (1000) items, and reading a
	// thousand Arrow tables up front to render twenty rows would be the aisheets mistake this
	// spec explicitly refuses. Row height is fixed and off-screen rows skip layout
	// (`content-visibility`), which carries a 1000-row grid without a virtualization library;
	// real windowing is phase-2 work if measured to matter.
	import { onDestroy } from 'svelte';
	import { base } from '$app/paths';
	import { loadAnnotations } from '@rask/labeling/annotations-client';
	import { Badge } from '@rask/ui/badge';
	import MediaThumb from '$lib/viewer/layout/MediaThumb.svelte';
	import { statusVariant } from '$lib/viewer/layout/statusStyle';
	import { taskCanvasHref } from '$lib/viewer/task-stream';
	import { fetchCorpusRows } from '$lib/projects/remote/rows.remote';
	import { indexRows, rowFor, rowKeysFor, rowText } from '$lib/projects/corpus-rows';
	import type { TaskDetail } from '$lib/projects/types';
	import { type AnnotationSummary, summarize } from './summary.js';
	import { SvelteMap } from 'svelte/reactivity';

	let { projectId, tasks }: { projectId: string; tasks: TaskDetail[] } = $props();

	// Corpus facets are DECORATION on a grid that works without them (the queue's own rule):
	// `?? []` everywhere, never a loading gate.
	const rowsQuery = $derived(fetchCorpusRows({ keys: rowKeysFor(tasks), dataset: null }));
	const rowIndex = $derived(
		indexRows(rowsQuery.current?.rows ?? [], rowsQuery.current?.keyFields ?? []),
	);
	const rowKeyFields = $derived(rowsQuery.current?.keyFields ?? []);

	// The lazy annotation-state cache, keyed by task id. `null` = fetch failed (rendered as an
	// honest "—", never retried in a loop); absent = not yet visible.
	const summaries = new SvelteMap<string, AnnotationSummary | null>();
	const inflight = new Set<string>();

	function annotationsUrl(task: TaskDetail): string | null {
		const key = (task.source.keys ?? []).join(',');
		if (!key) return null;
		const ds = task.source.where ? `?dataset=${encodeURIComponent(task.source.where)}` : '';
		return `${base}/api/annotations/${key}${ds}`;
	}

	async function fetchSummary(task: TaskDetail): Promise<void> {
		const url = annotationsUrl(task);
		if (!url || summaries.has(task.task_id) || inflight.has(task.task_id)) return;
		inflight.add(task.task_id);
		try {
			const { table } = await loadAnnotations(url);
			summaries.set(task.task_id, summarize(table));
		} catch {
			summaries.set(task.task_id, null);
		} finally {
			inflight.delete(task.task_id);
		}
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
			</tr>
		</thead>
		<tbody>
			{#each tasks as task (task.task_id)}
				{@const summary = summaries.get(task.task_id)}
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
							<span class="flex flex-wrap gap-1">
								{#each Object.entries(summary.byStatus) as [status, count] (status)}
									<Badge variant={statusVariant(status)} class="text-[10px]">
										{status}
										{count}
									</Badge>
								{/each}
								{#if summary.total === 0}
									<span class="text-muted-foreground">empty</span>
								{/if}
							</span>
						{/if}
					</td>
					<td class="px-2 py-1" data-testid="bulk-tags">
						{#if summary}
							<span class="flex flex-wrap gap-1">
								{#each summary.tags as tag (tag)}
									<Badge variant="secondary" class="text-[10px]">{tag}</Badge>
								{/each}
							</span>
						{/if}
					</td>
					<td
						class="text-muted-foreground max-w-64 truncate px-2 py-1 text-[11px]"
						data-testid="bulk-text"
					>
						{summary?.text || ''}
					</td>
				</tr>
			{/each}
		</tbody>
	</table>
	{#if tasks.length === 0}
		<p class="text-muted-foreground p-4 text-sm">No items in this labeling task yet.</p>
	{/if}
</div>
