<script lang="ts">
	/** Persistent right panel. Click a node → see its inputs + interatchte
	 *  results; click a result → play it here (reuses PlayerPane). */
	import { ArrowLeft, Copy, Download, Eye, EyeOff } from '@lucide/svelte';
	import { activeView } from '@rask/media-api';
	import {
		graph,
		modeLabel,
		nodeLabel,
		RERANK_TOP_N,
		STATUS_DOT,
	} from '$lib/workflow/graph.svelte';
	import { exportHits, exportColumns } from '$lib/workflow/export';
	import PlayerPane from '$lib/components/player-pane.svelte';
	import HitList from '$lib/workflow/HitList.svelte';

	const id = $derived(graph.inspectedNodeId);
	const kind = $derived(id ? graph.kindOf(id) : null);
	const cfg = $derived(id ? graph.config[id] : null);
	const rt = $derived(id ? graph.runtime[id] : null);
	const hits = $derived(rt?.hits ?? []);

	const EXPORT_FORMATS = ['csv', 'json'] as const;

	// Every column the active dataset offers, and the current selection (a `null`
	// config means "all"), both derived from the descriptor.
	const allColumns = $derived(exportColumns());
	const selectedColumns = $derived(cfg ? (cfg.exportColumns ?? allColumns) : []);

	/** Toggle one export column, keeping the selection in canonical column order. */
	function toggleExportColumn(nodeId: string, current: readonly string[], col: string): void {
		const has = current.includes(col);
		const next = exportColumns().filter((c) => (c === col ? !has : current.includes(c)));
		graph.setConfig(nodeId, { exportColumns: next });
	}

	const title = $derived(
		kind === 'search' && cfg ? `Search · ${modeLabel(cfg.mode)}` : kind ? nodeLabel(kind) : '',
	);

	const statusText = $derived.by((): string => {
		if (!rt) return '';
		if (rt.status === 'running') return 'searching…';
		if (rt.status === 'error') return 'error';
		if (rt.status === 'done') return rt.count != null ? `done · ${rt.count} hits` : 'done';
		return 'not run yet';
	});

	// Per-kind input/config summary (only the fields that matter for that kind).
	const rows = $derived.by((): [string, string][] => {
		if (!cfg || !kind) return [];
		const r: [string, string][] = [];
		if (kind === 'query') r.push(['Query', cfg.q || '—']);
		if (kind === 'image') r.push(['Image', cfg.imageName || '(none uploaded)']);
		if (kind === 'filter') {
			if (cfg.where) r.push(['Where', cfg.where]);
			const view = activeView();
			for (const field of view.filterFields) {
				const value = cfg.filters[field];
				if (value) {
					const label = view.metadataFields.find((m) => m.field === field)?.label ?? field;
					r.push([label, value]);
				}
			}
			if (!r.length) r.push(['Filter', '(empty)']);
		}
		if (kind === 'search') {
			r.push(['Mode', modeLabel(cfg.mode)]);
			r.push(['Query', cfg.q || (cfg.mode === 'visual' ? '(from image)' : '—')]);
			r.push(['Results', String(cfg.n)]);
			if (cfg.rerank) r.push(['Rerank', `top ${RERANK_TOP_N}`]);
			// Only surface the refine granularity once a scope was actually applied
			// (an upstream result fed in) — the SearchNode toggle shows the setting
			// itself, so a standalone row here would read as a scope when there's none.
			if (rt?.scopedDocs)
				r.push(['Scope', `within ${rt.scopedDocs} videos${rt.scopeCapped ? ' (capped)' : ''}`]);
			if (rt?.scopedChunks)
				r.push(['Scope', `within ${rt.scopedChunks} chunks${rt.scopeCapped ? ' (capped)' : ''}`]);
			if (rt?.ms != null) r.push(['Time', `${rt.ms} ms`]);
		}
		if (kind === 'combine') {
			r.push(['Combine', cfg.combineMode === 'intersect' ? 'intersect (∩)' : 'union (∪)']);
		}
		if (kind === 'tagger') {
			r.push(['Tags', cfg.tags.length ? cfg.tags.join(', ') : '(none — add some)']);
		}
		return r;
	});
</script>

<div data-testid="inspector" class="border-border bg-card flex h-full min-h-0 flex-col border-l">
	<header class="border-border flex h-11 shrink-0 items-center gap-2 border-b px-3">
		{#if graph.selectedHit}
			<button
				type="button"
				onclick={() => graph.closeDetail()}
				aria-label="Back to results"
				class="text-muted-foreground hover:bg-muted hover:text-foreground rounded-md p-1 transition-colors"
			>
				<ArrowLeft class="size-4" />
			</button>
			<span class="text-foreground truncate text-sm font-medium">
				{activeView().title(graph.selectedHit) || 'Now playing'}
			</span>
		{:else}
			<span class="text-foreground text-sm font-medium">Inspector</span>
			{#if title}<span class="text-muted-foreground truncate text-xs">· {title}</span>{/if}
		{/if}
	</header>

	<div class="min-h-0 flex-1 overflow-y-auto">
		{#if graph.selectedHit}
			<PlayerPane hit={graph.selectedHit} />
		{:else if id && kind && cfg && rt}
			<div class="flex flex-col gap-3 p-3 text-xs">
				<div class="flex items-center gap-1.5">
					<input
						class="border-border bg-background text-foreground focus:border-primary min-w-0 flex-1 rounded-md border px-2 py-1 text-xs outline-none"
						placeholder={title}
						aria-label="Rename node"
						bind:value={cfg.label}
					/>
					<button
						type="button"
						onclick={() => graph.duplicateNode(id)}
						title="Duplicate node"
						aria-label="Duplicate node"
						class="text-muted-foreground hover:bg-muted hover:text-foreground shrink-0 rounded-md p-1 transition-colors"
					>
						<Copy class="size-3.5" />
					</button>
					<button
						type="button"
						onclick={() => graph.setConfig(id, { enabled: !cfg.enabled })}
						title={cfg.enabled ? 'Disable (bypass) node' : 'Enable node'}
						aria-label="Toggle node enabled"
						class="text-muted-foreground hover:bg-muted hover:text-foreground shrink-0 rounded-md p-1 transition-colors"
					>
						{#if cfg.enabled}<EyeOff class="size-3.5" />{:else}<Eye class="size-3.5" />{/if}
					</button>
				</div>
				<div class="flex items-center gap-2">
					<span class="size-2 shrink-0 rounded-full {STATUS_DOT[rt.status]}"></span>
					<span class="text-muted-foreground">{statusText}</span>
				</div>
				{#if rt.error}
					<div
						class="border-destructive/30 bg-destructive/10 text-destructive rounded-md border p-2"
					>
						{rt.error}
					</div>
				{/if}

				<dl class="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
					{#each rows as [label, value] (label)}
						<dt class="text-muted-foreground">{label}</dt>
						<dd class="text-foreground font-medium break-words">{value}</dd>
					{/each}
				</dl>

				{#if kind === 'export'}
					<!-- Configure exactly what the Export node downloads: format + which
               chunk columns (both JSON and CSV honour the column selection). -->
					<div class="border-border flex flex-col gap-2.5 border-t pt-3">
						<div class="flex items-center gap-2">
							<span class="text-muted-foreground">Format</span>
							<div class="border-border flex overflow-hidden rounded-md border">
								{#each EXPORT_FORMATS as fmt (fmt)}
									<button
										type="button"
										class="px-2.5 py-0.5 text-xs font-medium transition-colors {cfg.exportFormat ===
										fmt
											? 'bg-primary text-primary-foreground'
											: 'text-muted-foreground hover:bg-muted'}"
										onclick={() => graph.setConfig(id, { exportFormat: fmt })}
									>
										{fmt.toUpperCase()}
									</button>
								{/each}
							</div>
						</div>

						<div>
							<div class="mb-1 flex items-center justify-between">
								<span class="text-muted-foreground text-[0.7rem] tracking-wide uppercase">
									Columns ({selectedColumns.length}/{allColumns.length})
								</span>
								<div class="flex gap-2 text-[0.7rem]">
									<button
										type="button"
										class="text-primary hover:underline"
										onclick={() => graph.setConfig(id, { exportColumns: null })}
									>
										All
									</button>
									<button
										type="button"
										class="text-primary hover:underline"
										onclick={() => graph.setConfig(id, { exportColumns: [] })}
									>
										None
									</button>
								</div>
							</div>
							<div class="grid grid-cols-2 gap-x-3 gap-y-0.5">
								{#each allColumns as col (col)}
									<label class="text-foreground flex items-center gap-1.5 text-xs">
										<input
											type="checkbox"
											class="accent-primary size-3"
											checked={selectedColumns.includes(col)}
											onchange={() => toggleExportColumn(id, selectedColumns, col)}
										/>
										<span class="truncate" title={col}>{col}</span>
									</label>
								{/each}
							</div>
						</div>

						<button
							type="button"
							class="border-border bg-background text-foreground hover:bg-muted inline-flex items-center justify-center gap-1.5 rounded-md border px-2 py-1.5 text-xs font-medium transition-colors disabled:opacity-50"
							disabled={hits.length === 0 || selectedColumns.length === 0}
							onclick={() =>
	exportHits(
		graph.tags.withTags(hits),
		cfg.exportFormat,
		new Date(),
		cfg.exportColumns,
	)}
						>
							<Download class="size-3.5" />
							Download {hits.length} hit{hits.length === 1 ? '' : 's'} as {cfg.exportFormat.toUpperCase()}
						</button>
					</div>
				{/if}

				{#if hits.length}
					<div>
						<div class="text-muted-foreground mb-1 text-[0.7rem] tracking-wide uppercase">
							Results ({hits.length}) · click to play
						</div>
						<HitList {hits} maxHeight="max-h-none" />
					</div>
				{:else if kind === 'search' || kind === 'results' || kind === 'export' || kind === 'combine' || kind === 'tagger'}
					<p class="text-muted-foreground text-xs">
						{#if kind === 'export' && rt.status === 'idle'}
							Press Run to feed results. Selected columns{selectedColumns.includes('tags')
				? ' (including tags)'
				: ''}
							will export.
						{:else if rt.status === 'idle'}
							Not run yet — press Run.
						{:else}
							No results.
						{/if}
					</p>
				{:else}
					<p class="text-muted-foreground text-xs">
						Produces a {nodeLabel(kind).toLowerCase()} input — wire it into a Search and Run.
					</p>
				{/if}
			</div>
		{:else}
			<div class="text-muted-foreground grid h-full place-items-center p-6 text-center text-xs">
				Click a node to inspect its inputs &amp; results — or click a result to play it.
			</div>
		{/if}
	</div>
</div>
