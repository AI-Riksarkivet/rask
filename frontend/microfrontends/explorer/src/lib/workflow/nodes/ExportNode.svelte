<script lang="ts">
	/** Sink node: collects the incoming Hit[] and downloads them as JSON/CSV with
	 *  the columns chosen in the Inspector. Format + columns are configured there;
	 *  the node itself shows a summary and a one-click Download. */
	import { Handle, Position, type NodeProps } from '@xyflow/svelte';
	import { Download, Tags } from '@lucide/svelte';
	import { activeView } from '@rask/explorer-api';
	import { graph } from '$lib/workflow/graph.svelte';
	import { exportColumns, exportHits } from '$lib/workflow/export';
	import { tagBatchFromTaggedHits, tagRemovesFromEntries } from '@rask/labeling/tag-writer';
	import { saveTagsAsAnnotations } from '$lib/workflow/remote/labeling.remote';
	import NodeShell from './NodeShell.svelte';

	let { id, selected }: NodeProps = $props();
	const rt = $derived(graph.runtime[id]);
	const cfg = $derived(graph.config[id]);
	const hits = $derived(rt?.hits ?? []);
	// Resolve the stored selection (`null` = every column the dataset offers).
	const cols = $derived(cfg ? (cfg.exportColumns ?? exportColumns()) : []);
	const canDownload = $derived(hits.length > 0 && cols.length > 0);

	// Tag→annotation unification: persist the run's chunk-tags as real annotation ROWS
	// (one merge_insert version) so they're reviewable in the annotator, not just an
	// export column. Only tagged rows are sent (idempotent by deterministic id).
	const taggedCount = $derived(hits.filter((h) => graph.tags.forHit(h).length > 0).length);
	// Un-tags queue independently of the visible hits — a removes-only save must stay reachable.
	const removedCount = $derived(graph.tags.removedEntries().length);
	let tagMsg = $state<string | null>(null);
	let tagSaving = $state(false);
	async function saveTags(): Promise<void> {
		if (tagSaving) return; // double-click guard — one batch, one Lance version
		const batch = tagBatchFromTaggedHits(hits, activeView().keyFields, (h) => graph.tags.forHit(h));
		// Snapshot the queue at build time: only THESE entries are flushed on success,
		// so an un-tag queued while this save is in flight survives for the next one.
		const sentRemoved = graph.tags.removedEntries();
		batch.removes = tagRemovesFromEntries(sentRemoved, activeView().keyFields);
		if (!batch.adds.length && !batch.removes.length) {
			tagMsg = 'no tags to save';
			return;
		}
		tagSaving = true;
		tagMsg = 'saving…';
		try {
			const res = await saveTagsAsAnnotations({
				adds: batch.adds,
				removes: batch.removes,
				dataset: activeView().datasetParam(),
			});
			if (!res.ok) {
				// A refused save must NOT flush the removes queue — those un-tags are still unsaved.
				tagMsg = res.detail;
				return;
			}
			graph.tags.flushRemoved(sentRemoved);
			tagMsg = `saved ${res.data.saved} → v${res.data.version}`;
		} catch (e) {
			tagMsg = e instanceof Error ? e.message : 'save failed';
		} finally {
			tagSaving = false;
		}
	}
</script>

{#if rt && cfg}
	<NodeShell {id} title="Export" status={rt.status} {selected}>
		<Handle type="target" position={Position.Left} />
		<div class="flex flex-col gap-2">
			<div class="text-muted-foreground text-xs">
				{#if hits.length}
					<span class="text-foreground">{hits.length}</span> hits ·
					{cols.length} col{cols.length === 1 ? '' : 's'} ·
					{cfg.exportFormat.toUpperCase()}
					{#if cols.includes('tags')}<span class="text-primary">+ tags</span>{/if}
				{:else if rt.status === 'idle'}
					Run the graph to feed this export.
				{:else}
					No results — check upstream nodes.
				{/if}
			</div>
			<button
				type="button"
				class="nodrag border-border bg-background text-foreground hover:bg-muted inline-flex items-center justify-center gap-1.5 rounded-md border px-2 py-1 text-xs font-medium transition-colors disabled:opacity-50"
				disabled={!canDownload}
				onclick={(e) => {
	e.stopPropagation();
	exportHits(graph.tags.withTags(hits), cfg.exportFormat, new Date(), cfg.exportColumns);
}}
			>
				<Download class="size-3" />
				Download {cfg.exportFormat.toUpperCase()}
			</button>

			<button
				type="button"
				class="nodrag border-border bg-background text-foreground hover:bg-muted inline-flex items-center justify-center gap-1.5 rounded-md border px-2 py-1 text-xs font-medium transition-colors disabled:opacity-50"
				disabled={tagSaving || (taggedCount === 0 && removedCount === 0)}
				title="Persist the run's chunk-tags as reviewable annotation rows"
				onclick={(e) => {
	e.stopPropagation();
	void saveTags();
}}
			>
				<Tags class="size-3" />
				Save {taggedCount || ''} tag{taggedCount === 1 ? '' : 's'} as annotations
			</button>
			{#if tagMsg}
				<div class="text-muted-foreground font-mono text-[0.7rem]">{tagMsg}</div>
			{/if}
		</div>
	</NodeShell>
{/if}
