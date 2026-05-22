<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import {
		searchLines,
		searchStats,
		searchCatalog,
		catalogStats,
		type SearchHit,
		type SearchStats,
		type CatalogHit,
	} from '$lib/api';
	import RayShell from '$lib/components/layout/ray-shell.svelte';
	import { Card } from '$lib/components/ui/card';
	import { Badge } from '$lib/components/ui/badge';
	import { Search, Loader2, Copy, Check, ExternalLink } from 'lucide-svelte';

	type Scope = 'lines' | 'catalog';

	let scope = $state<Scope>('lines');
	let q = $state('');
	let limit = $state(50);
	// Lines side
	let hits = $state<SearchHit[]>([]);
	let count = $state(0);
	let stats = $state<SearchStats | null>(null);
	// Catalog side
	let catalogHits = $state<CatalogHit[]>([]);
	let catalogCount = $state(0);
	let catalogStatsState = $state<SearchStats | null>(null);
	// Filter the catalog tab to a minimum local-state tier. 'all' shows
	// everything, 'listed' / 'cached' / 'transcribed' progressively narrow
	// to volumes we know about, have downloaded, or have HTR'd. Pure client-
	// side over the already-fetched page.
	type LocalFilter = 'all' | 'listed' | 'cached' | 'transcribed';
	let localFilter = $state<LocalFilter>('all');
	let visibleCatalogHits = $derived.by(() => {
		switch (localFilter) {
			case 'transcribed':
				return catalogHits.filter((h) => h.transcribed);
			case 'cached':
				return catalogHits.filter((h) => h.cached);
			case 'listed':
				return catalogHits.filter((h) => h.listed);
			default:
				return catalogHits;
		}
	});

	let elapsed = $state<number | null>(null);
	let loading = $state(false);
	let error = $state<string | null>(null);
	// Snapshot of the query at the moment of the last successful search — used
	// to highlight matched tokens in result text. We can't read `q` directly
	// because the user may type a new query without re-running the search.
	let highlightedQuery = $state('');
	// Tracks which hit's thumbnail was just copied — used to flash a check icon
	// on the corresponding copy button. Cleared after a short timeout.
	let copiedKey = $state<string | null>(null);

	onMount(async () => {
		// Fetch both stats so the active-tab badge always shows the right number
		// when the user toggles scope, without paying a fetch on every switch.
		try {
			stats = await searchStats();
		} catch {
			stats = { available: false, rows: 0 };
		}
		try {
			catalogStatsState = await catalogStats();
		} catch {
			catalogStatsState = { available: false, rows: 0 };
		}
	});

	function setScope(s: Scope) {
		if (scope === s) return;
		scope = s;
		// Don't auto-rerun — the user may want to retype. Just clear the result
		// counters so the previous tab's hit count doesn't leak across.
		elapsed = null;
		error = null;
	}

	async function runSearch(e?: SubmitEvent) {
		e?.preventDefault();
		const query = q.trim();
		if (!query) {
			if (scope === 'lines') {
				hits = [];
				count = 0;
			} else {
				catalogHits = [];
				catalogCount = 0;
			}
			return;
		}
		loading = true;
		error = null;
		const t0 = performance.now();
		try {
			if (scope === 'lines') {
				const r = await searchLines(query, limit);
				hits = r.hits;
				count = r.count;
			} else {
				const r = await searchCatalog(query, limit);
				catalogHits = r.hits;
				catalogCount = r.count;
			}
			highlightedQuery = query;
			elapsed = performance.now() - t0;
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
			if (scope === 'lines') {
				hits = [];
				count = 0;
			} else {
				catalogHits = [];
				catalogCount = 0;
			}
		} finally {
			loading = false;
		}
	}

	function hitKey(h: SearchHit): string {
		return `${h.line_id}:${h.batch_id}`;
	}

	async function copyImage(e: MouseEvent, h: SearchHit) {
		// Stop the card's onclick from firing, otherwise we'd navigate away.
		e.stopPropagation();
		if (!h.thumb_url) return;
		try {
			const res = await fetch(h.thumb_url);
			const blob = await res.blob();
			// ClipboardItem only reliably accepts image/png — JPEG round-trips via canvas.
			const png = blob.type === 'image/png' ? blob : await jpegToPng(blob);
			await navigator.clipboard.write([new ClipboardItem({ 'image/png': png })]);
			const k = hitKey(h);
			copiedKey = k;
			setTimeout(() => {
				if (copiedKey === k) copiedKey = null;
			}, 1500);
		} catch (err) {
			console.error('copy failed', err);
		}
	}

	async function jpegToPng(blob: Blob): Promise<Blob> {
		const bmp = await createImageBitmap(blob);
		const canvas = document.createElement('canvas');
		canvas.width = bmp.width;
		canvas.height = bmp.height;
		const ctx = canvas.getContext('2d');
		if (!ctx) throw new Error('no 2d context');
		ctx.drawImage(bmp, 0, 0);
		return new Promise((resolve, reject) =>
			canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('toBlob failed'))), 'image/png'),
		);
	}

	function onCardKey(e: KeyboardEvent, h: SearchHit) {
		if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			gotoHit(h);
		}
	}

	function gotoHit(h: SearchHit) {
		// Viewer route is /viewer/{volume}/{page}, where {page} is the full
		// listPages key — `<batch_id>/<filename>` (e.g. "A0060198/A0060198_00021.jpg").
		// The slash gets URL-encoded so it stays in one path segment.
		const pageKey = `${h.batch_id}/${h.page_id}`;
		goto(
			`/viewer/${encodeURIComponent(h.batch_id)}/${encodeURIComponent(pageKey)}` +
				`?line=${encodeURIComponent(h.line_id)}`,
		);
	}

	function fmtConf(c: number): string {
		return c.toFixed(2);
	}

	function fmtDate(h: CatalogHit): string {
		// Prefer the human-readable date_text (e.g. "1798" or "1720--1889") when
		// present. Fall back to the integer date_start/date_end pair, which is
		// what we get from messy or compound date strings.
		if (h.date_text) return h.date_text;
		if (h.date_start && h.date_end) {
			return h.date_start === h.date_end
				? String(h.date_start)
				: `${h.date_start}–${h.date_end}`;
		}
		return '';
	}

	function confColor(c: number): string {
		if (c >= 0.85) return 'bg-emerald-500/20 text-emerald-700 dark:text-emerald-300';
		if (c >= 0.5) return 'bg-amber-500/20 text-amber-700 dark:text-amber-300';
		return 'bg-rose-500/20 text-rose-700 dark:text-rose-300';
	}

	/** Split `text` into segments matching/not-matching any token in `query`.
	 * Case-insensitive whole-substring match (the FTS itself is token-based,
	 * so this covers what the index returns). */
	function splitForHighlight(text: string, query: string): { text: string; match: boolean }[] {
		const tokens = query
			.toLowerCase()
			.split(/\s+/)
			.filter(Boolean);
		if (tokens.length === 0) return [{ text, match: false }];
		const escaped = tokens.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
		const re = new RegExp(`(${escaped.join('|')})`, 'gi');
		const out: { text: string; match: boolean }[] = [];
		let last = 0;
		for (const m of text.matchAll(re)) {
			const i = m.index!;
			if (i > last) out.push({ text: text.slice(last, i), match: false });
			out.push({ text: m[0], match: true });
			last = i + m[0].length;
		}
		if (last < text.length) out.push({ text: text.slice(last), match: false });
		return out;
	}
</script>

<svelte:head>
	<title>Search — RASK</title>
</svelte:head>

<RayShell title="Search">
	<div class="flex flex-col gap-4 p-6 text-sm">
		<!-- Scope tabs — `Lines` runs FTS over transcribed text, `Catalog` over
		     EAD volume metadata (archive hierarchy + dates + descriptions). The
		     two indexes have different schemas and result rendering, so the
		     toggle just flips which API + card layout we use. -->
		<div class="flex items-center gap-1 border-b border-border">
			<button
				type="button"
				class="border-b-2 px-3 py-2 text-sm font-medium transition {scope === 'lines'
					? 'border-primary text-foreground'
					: 'border-transparent text-muted-foreground hover:text-foreground'}"
				onclick={() => setScope('lines')}>Lines</button
			>
			<button
				type="button"
				class="border-b-2 px-3 py-2 text-sm font-medium transition {scope === 'catalog'
					? 'border-primary text-foreground'
					: 'border-transparent text-muted-foreground hover:text-foreground'}"
				onclick={() => setScope('catalog')}>Catalog</button
			>
		</div>

		<form onsubmit={runSearch} class="flex items-center gap-2">
			<div class="relative flex-1">
				<Search class="absolute top-2.5 left-3 h-4 w-4 text-muted-foreground" />
				<input
					type="text"
					bind:value={q}
					placeholder={scope === 'lines'
						? 'search transcribed lines… (FTS — exact tokens)'
						: 'search archive catalog (volume titles, descriptions, dates)…'}
					class="w-full rounded-md border border-input bg-background py-2 pr-3 pl-9 text-sm outline-none ring-offset-background focus-visible:ring-2 focus-visible:ring-ring"
				/>
			</div>
			<select
				bind:value={limit}
				class="rounded-md border border-input bg-background px-2 py-2 text-sm"
			>
				<option value={20}>20</option>
				<option value={50}>50</option>
				<option value={100}>100</option>
				<option value={250}>250</option>
			</select>
			<button
				type="submit"
				class="rounded-md bg-primary px-4 py-2 text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
				disabled={loading || !q.trim()}
			>
				{#if loading}<Loader2 class="h-4 w-4 animate-spin" />{:else}Search{/if}
			</button>
		</form>

		<div class="flex items-center gap-3 text-xs text-muted-foreground">
			{#if scope === 'lines' && stats}
				<Badge variant="secondary">
					{stats.available ? `${stats.rows.toLocaleString()} lines indexed` : 'index unavailable'}
				</Badge>
			{:else if scope === 'catalog' && catalogStatsState}
				<Badge variant="secondary">
					{catalogStatsState.available
						? `${catalogStatsState.rows.toLocaleString()} volumes indexed`
						: 'catalog unavailable'}
				</Badge>
			{/if}
			{#if elapsed !== null}
				{@const n = scope === 'lines' ? count : catalogCount}
				{#if n > 0}
					<span>
						{n} hit{n === 1 ? '' : 's'}
						{#if scope === 'catalog' && localFilter !== 'all'}
							· {visibleCatalogHits.length} {localFilter}
						{/if}
						in {elapsed.toFixed(0)} ms
					</span>
				{/if}
			{/if}
			{#if scope === 'catalog'}
				<label class="ml-auto flex cursor-pointer items-center gap-1.5 select-none">
					<span>Show:</span>
					<select
						bind:value={localFilter}
						class="cursor-pointer rounded border border-input bg-background px-1.5 py-0.5 text-xs"
					>
						<option value="all">all</option>
						<option value="listed">listed</option>
						<option value="cached">cached</option>
						<option value="transcribed">transcribed</option>
					</select>
				</label>
			{/if}
		</div>

		{#if error}
			<Card class="border-destructive/40 bg-destructive/10 p-3 text-destructive">{error}</Card>
		{/if}

		{#if scope === 'lines'}
			{#if hits.length > 0}
				<div class="flex flex-col gap-2">
					{#each hits as h (h.line_id + ':' + h.batch_id)}
						<div
							role="button"
							tabindex="0"
							class="flex cursor-pointer flex-col gap-2 rounded-md border bg-card p-3 text-left transition hover:border-primary/50 hover:bg-accent/30 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
							onclick={() => gotoHit(h)}
							onkeydown={(e) => onCardKey(e, h)}
						>
							{#if h.thumb_url}
								<!-- Full natural width — line crops are stored at 64 px tall and
								     proportional width, which can be ~500-1500 px wide. The container
								     scrolls horizontally if the line is wider than the card. The
								     polygon overlay is drawn in the line's original page-space
								     (viewBox = "hpos vpos width height"), so it auto-aligns with
								     the cropped image. `non-scaling-stroke` keeps the line crisp
								     regardless of zoom. -->
								<div class="relative overflow-x-auto rounded border bg-background">
									<button
										type="button"
										class="absolute top-1.5 right-1.5 z-10 rounded bg-background/85 p-1.5 text-muted-foreground shadow-sm backdrop-blur-sm transition hover:bg-background hover:text-foreground"
										onclick={(e) => copyImage(e, h)}
										title="Copy image to clipboard"
										aria-label="Copy image to clipboard"
									>
										{#if copiedKey === hitKey(h)}
											<Check class="h-4 w-4 text-emerald-600" />
										{:else}
											<Copy class="h-4 w-4" />
										{/if}
									</button>
									<div class="relative inline-block leading-none">
										<img
											src={h.thumb_url}
											alt={h.text}
											loading="lazy"
											class="block h-16 max-w-none"
										/>
										{#if h.polygon}
											<svg
												class="pointer-events-none absolute inset-0 h-full w-full"
												viewBox="{h.hpos} {h.vpos} {h.width} {h.height}"
												preserveAspectRatio="none"
											>
												<polygon
													points={h.polygon}
													fill="rgba(244,63,94,0.10)"
													stroke="rgba(244,63,94,0.85)"
													stroke-width="2"
													vector-effect="non-scaling-stroke"
												/>
											</svg>
										{/if}
									</div>
								</div>
							{/if}
							<div class="flex items-center gap-3">
								<div class="min-w-0 flex-1">
									<div class="truncate font-mono text-base">
										{#each splitForHighlight(h.text, highlightedQuery) as seg}
											{#if seg.match}
												<mark class="rounded bg-amber-300/70 px-0.5 text-foreground"
													>{seg.text}</mark
												>
											{:else}{seg.text}{/if}
										{/each}
									</div>
									<!-- Catalog context — fonds title + date range + reference code.
									     Hidden when the batch isn't in the harvested EAD catalog
									     (e.g. test batches). The lower meta line keeps the click
									     coordinates (batch / page / line index). -->
									{#if h.catalog}
										<div class="mt-0.5 truncate text-xs">
											<span class="text-foreground">{h.catalog.fonds_title}</span>
											{#if h.catalog.date_text}
												<span class="text-muted-foreground"> · {h.catalog.date_text}</span>
											{/if}
											<span class="ml-1 font-mono text-muted-foreground"
												>{h.catalog.reference_code}</span
											>
										</div>
									{/if}
									<div class="mt-0.5 truncate text-[11px] text-muted-foreground">
										{h.batch_id} · {h.page_id} · L{h.line_idx}
									</div>
								</div>
								<Badge class={confColor(h.confidence)}>conf {fmtConf(h.confidence)}</Badge>
							</div>
						</div>
					{/each}
				</div>
			{:else if !loading && q.trim()}
				<Card class="p-6 text-center text-muted-foreground">No hits.</Card>
			{/if}
		{:else if visibleCatalogHits.length > 0}
			<!-- Catalog hits: each one is a digitised volume in some archive's
			     fonds/series hierarchy. If the volume is cached locally
			     (h.cached, set by the backend by joining bild_id against
			     batches.db) the card opens in our viewer; otherwise it links
			     out to Riksarkivet's bildvisning. The `cachedOnly` filter
			     hides non-cached rows entirely (see visibleCatalogHits). -->
			<div class="flex flex-col gap-2">
				{#each visibleCatalogHits as h (h.id)}
					{@const localHref = h.cached ? `/viewer/${encodeURIComponent(h.bild_id)}` : null}
					{@const externalHref = h.bildvisning_url || null}
					{@const accentClass = h.transcribed
						? 'border-emerald-500/40'
						: h.cached
							? 'border-blue-500/30'
							: h.listed
								? 'border-amber-500/30'
								: ''}
					<a
						href={localHref || externalHref || '#'}
						target={localHref ? undefined : '_blank'}
						rel={localHref ? undefined : 'noopener'}
						class="flex flex-col gap-2 rounded-md border bg-card p-3 text-left transition hover:border-primary/50 hover:bg-accent/30 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none {accentClass}"
					>
						<div class="flex items-start gap-3">
							<div class="min-w-0 flex-1">
								<div class="flex flex-wrap items-center gap-2 text-base font-medium">
									{#each splitForHighlight(h.fonds_title || '(untitled fonds)', highlightedQuery) as seg}
										{#if seg.match}<mark class="rounded bg-amber-300/70 px-0.5 text-foreground"
												>{seg.text}</mark
											>{:else}{seg.text}{/if}
									{/each}
									{#if h.series_title}
										<span class="text-muted-foreground">›</span>
										<span>
											{#each splitForHighlight(h.series_title, highlightedQuery) as seg}
												{#if seg.match}<mark
														class="rounded bg-amber-300/70 px-0.5 text-foreground"
														>{seg.text}</mark
													>{:else}{seg.text}{/if}
											{/each}
										</span>
									{/if}
									{#if h.volume_title}
										<span class="text-muted-foreground">›</span>
										<span class="font-mono text-sm">
											{#each splitForHighlight(h.volume_title, highlightedQuery) as seg}
												{#if seg.match}<mark
														class="rounded bg-amber-300/70 px-0.5 text-foreground"
														>{seg.text}</mark
													>{:else}{seg.text}{/if}
											{/each}
										</span>
									{/if}
								</div>
								{#if h.description}
									<div class="mt-1 line-clamp-2 text-sm text-muted-foreground">
										{#each splitForHighlight(h.description, highlightedQuery) as seg}
											{#if seg.match}<mark class="rounded bg-amber-300/70 px-0.5 text-foreground"
													>{seg.text}</mark
												>{:else}{seg.text}{/if}
										{/each}
									</div>
								{/if}
								<div class="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
									<Badge variant="secondary">{h.archive_code}</Badge>
									{#if fmtDate(h)}<span>{fmtDate(h)}</span>{/if}
									<span class="font-mono">{h.reference_code}</span>
									<!-- Local-state badge: only the highest tier the volume has reached
									     (transcribed > cached > listed). Skipped entirely for volumes
									     unknown to batches.db — those just route to Riksarkivet. -->
									{#if h.transcribed}
										<Badge
											class="bg-emerald-500/20 text-emerald-700 dark:text-emerald-300"
										>
											Transcribed
										</Badge>
									{:else if h.cached}
										<Badge class="bg-blue-500/20 text-blue-700 dark:text-blue-300">
											Cached
										</Badge>
									{:else if h.listed}
										<Badge class="bg-amber-500/20 text-amber-700 dark:text-amber-300">
											Listed
										</Badge>
									{/if}
								</div>
							</div>
							{#if localHref}
								<Search class="mt-1 h-4 w-4 shrink-0 text-emerald-600" />
							{:else if externalHref}
								<ExternalLink class="mt-1 h-4 w-4 shrink-0 text-muted-foreground" />
							{/if}
						</div>
					</a>
				{/each}
			</div>
		{:else if !loading && q.trim()}
			<!-- Distinguish "FTS returned nothing" from "hits exist but the
			     Show: filter hides them all" — the latter is recoverable by
			     switching back to "all". -->
			{#if scope === 'catalog' && localFilter !== 'all' && catalogHits.length > 0}
				<Card class="p-6 text-center text-muted-foreground">
					{catalogHits.length} hit{catalogHits.length === 1 ? '' : 's'}, none
					<span class="font-medium">{localFilter}</span>. Switch
					<span class="font-medium">Show</span> to <span class="font-medium">all</span> to see them.
				</Card>
			{:else}
				<Card class="p-6 text-center text-muted-foreground">No hits.</Card>
			{/if}
		{/if}
	</div>
</RayShell>
