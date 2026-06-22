<script lang="ts">
	import { onMount, untrack } from 'svelte';
	import { goto } from '$app/navigation';
	import { page as pageStore } from '$app/state';
	import RayShell from '$lib/components/layout/ray-shell.svelte';
	import { Button } from '@rask/ui/button';
	import { Badge } from '@rask/ui/badge';
	import {
		ChevronLeft,
		ChevronRight,
		Maximize,
		Type,
		BoxSelect,
		Spline,
		SunMedium,
		PanelRightClose,
		PanelRightOpen,
	} from 'lucide-svelte';
	import { CanvasController } from '$lib/canvas';
	import { listPages, imageUrl, fetchAlto, getBatchCatalog, type CatalogHit } from '@rask/api';
	import { parseAlto } from '$lib/alto';
	import type { AltoParse, Line, PageEntry } from '@rask/api';

	const volume = $derived(pageStore.params.volume!);
	const pageKey = $derived(pageStore.params.page!);
	// Set when arriving from /search — viewer highlights this line + auto-enables polygons.
	const highlightLineId = $derived(pageStore.url.searchParams.get('line'));

	let pages = $state<PageEntry[]>([]);
	// EAD catalog metadata for this batch (fonds/series/volume/dates/etc.).
	// null = not loaded yet OR no matching row in the catalog (e.g. test
	// batches not in the OAI-PMH harvest). Re-fetched whenever `volume`
	// changes via the $effect below.
	let catalog = $state<CatalogHit | null>(null);
	let alto = $state<AltoParse | null>(null);
	// Raw ALTO XML, retained alongside the parsed `alto` so the right pane can
	// render it as text. fetchAlto returns the string before parseAlto runs.
	let xmlText = $state<string | null>(null);
	let altoError = $state<string | null>(null);
	let view = $state<'lines' | 'xml'>('lines');
	let copied = $state(false);
	let hoveredLine = $state(-1);
	// Index of the line the user navigated to from /search. Drawn in
	// drawOverlay with a heavier outline + glow than the hover treatment so
	// it's spottable at full-page zoom.
	let searchHighlightLine = $state(-1);
	let canvasEl: HTMLCanvasElement | undefined = $state();
	let controller: CanvasController | undefined;
	let img: HTMLImageElement | null = null;
	let hasFitOnce = false;

	// Display toggles (persisted in localStorage so they survive nav).
	let showBoxes = $state(persisted('viewer.showBoxes', true));
	let showPolygons = $state(persisted('viewer.showPolygons', false));
	let showText = $state(persisted('viewer.showText', false));
	let imgFilter = $state(persisted<'none' | 'highContrast' | 'invert'>('viewer.imgFilter', 'none'));
	let showPanel = $state(persisted('viewer.showPanel', true));

	$effect(() => savePersisted('viewer.showBoxes', showBoxes));
	$effect(() => savePersisted('viewer.showPolygons', showPolygons));
	$effect(() => savePersisted('viewer.showText', showText));
	$effect(() => savePersisted('viewer.imgFilter', imgFilter));
	$effect(() => savePersisted('viewer.showPanel', showPanel));

	// Refetch catalog metadata whenever the user navigates to a different
	// batch. The fetch is fire-and-forget; failures (404 or otherwise) just
	// leave `catalog` null and the panel hides itself.
	$effect(() => {
		const v = volume;
		catalog = null;
		getBatchCatalog(v)
			.then((row) => {
				if (volume === v) catalog = row;
			})
			.catch(() => {
				if (volume === v) catalog = null;
			});
	});

	const idx = $derived(pages.findIndex((p) => p.key === pageKey));
	const prevPage = $derived(idx > 0 ? pages[idx - 1] : null);
	const nextPage = $derived(idx >= 0 && idx < pages.length - 1 ? pages[idx + 1] : null);

	const filterCss = $derived(
		imgFilter === 'highContrast'
			? 'contrast(1.4) brightness(1.05)'
			: imgFilter === 'invert'
				? 'invert(1) hue-rotate(180deg)'
				: 'none',
	);

	function persisted<T>(key: string, fallback: T): T {
		if (typeof localStorage === 'undefined') return fallback;
		const raw = localStorage.getItem(key);
		if (raw === null) return fallback;
		try {
			return JSON.parse(raw) as T;
		} catch {
			return fallback;
		}
	}

	function savePersisted(key: string, value: unknown): void {
		if (typeof localStorage === 'undefined') return;
		localStorage.setItem(key, JSON.stringify(value));
	}

	$effect(() => {
		const v = volume;
		untrack(() => loadPages(v));
	});

	$effect(() => {
		const v = volume,
			k = pageKey;
		untrack(() => loadPage(v, k));
	});

	// Re-render when toggles change
	$effect(() => {
		void showBoxes;
		void showPolygons;
		void showText;
		void hoveredLine;
		controller?.render();
	});

	// Search → viewer: when the URL carries `?line=<line_id>`, find the matching
	// TextLine after ALTO loads, force polygons on, and apply a prominent
	// "you came from search" highlight (separate state from `hoveredLine` so
	// hovering elsewhere doesn't erase it).
	$effect(() => {
		const lid = highlightLineId;
		const a = alto;
		if (!lid || !a) {
			searchHighlightLine = -1;
			return;
		}
		const idx = a.lines.findIndex((l) => l.altoId === lid);
		searchHighlightLine = idx >= 0 ? idx : -1;
		if (idx >= 0) {
			// Hide the per-line boxes/polygons so only the search hit's
			// highlight stands out — the other lines would just clutter.
			showBoxes = false;
			showPolygons = false;
		}
	});

	// Re-render whenever the search highlight changes too.
	$effect(() => {
		void searchHighlightLine;
		controller?.render();
	});

	// Distinct hue per line — evenly spaced around the wheel.
	// Golden-angle (~137°) gives the maximum visual separation between adjacent indices.
	function lineColor(i: number, alpha: number): string {
		const hue = (i * 137.508) % 360;
		return `hsla(${hue}, 70%, 55%, ${alpha})`;
	}

	async function loadPages(v: string) {
		try {
			pages = await listPages(v);
		} catch (e) {
			console.error('listPages', e);
			pages = [];
		}
	}

	async function loadPage(v: string, k: string) {
		altoError = null;
		xmlText = null;
		hoveredLine = -1;

		// Load image — keep the previous frame visible until the new one is ready
		// to avoid the "blink" between pages.
		const newImg = new Image();
		newImg.onload = () => {
			img = newImg;
			if (controller) {
				controller.setImage(newImg);
				if (!hasFitOnce) {
					controller.fitToCanvas();
					hasFitOnce = true;
				}
				controller.render();
			}
		};
		newImg.src = imageUrl(v, k);

		// Load ALTO in parallel with image
		try {
			const xml = await fetchAlto(v, k);
			xmlText = xml ?? null;
			alto = xml ? parseAlto(xml) : null;
		} catch (e) {
			altoError = e instanceof Error ? e.message : String(e);
			xmlText = null;
			alto = null;
		}
	}

	async function copyXml() {
		if (!xmlText) return;
		try {
			await navigator.clipboard.writeText(xmlText);
			copied = true;
			setTimeout(() => (copied = false), 1500);
		} catch (e) {
			console.error('clipboard write failed', e);
		}
	}

	onMount(() => {
		if (!canvasEl) return;
		controller = new CanvasController(canvasEl, {
			onAfterDraw: drawOverlay,
		});
		if (img) controller.setImage(img);
		canvasEl.addEventListener('pointermove', onCanvasMove);
		return () => {
			controller?.destroy();
			canvasEl?.removeEventListener('pointermove', onCanvasMove);
		};
	});

	function drawOverlay(ctx: CanvasRenderingContext2D) {
		if (!alto) return;
		ctx.save();
		const lines = alto.lines;

		// Search-arrival highlight first (drawn under the per-line overlays so
		// the box/polygon strokes layer over it). A wide rose-coloured ring
		// with a soft shadow stays visible even at full-page zoom.
		if (searchHighlightLine >= 0 && searchHighlightLine < lines.length) {
			const sb = lines[searchHighlightLine].bbox;
			ctx.save();
			ctx.shadowColor = 'rgba(244, 63, 94, 0.7)'; // rose-500
			ctx.shadowBlur = 24;
			ctx.lineWidth = 6;
			ctx.strokeStyle = 'rgba(244, 63, 94, 0.95)';
			ctx.strokeRect(sb.x - 3, sb.y - 3, sb.w + 6, sb.h + 6);
			ctx.restore();
		}

		for (let i = 0; i < lines.length; i++) {
			const line = lines[i];
			const b = line.bbox;
			const isHover = i === hoveredLine;

			if (showBoxes) {
				ctx.lineWidth = isHover ? 2 : 1.2;
				ctx.strokeStyle = isHover ? '#f59e0b' : lineColor(i, 0.7);
				ctx.strokeRect(b.x, b.y, b.w, b.h);
			}

			if (showPolygons && line.polygon && line.polygon.length > 1) {
				ctx.beginPath();
				ctx.moveTo(line.polygon[0].x, line.polygon[0].y);
				for (let p = 1; p < line.polygon.length; p++) {
					ctx.lineTo(line.polygon[p].x, line.polygon[p].y);
				}
				ctx.closePath();
				ctx.fillStyle = isHover ? 'rgba(245, 158, 11, 0.4)' : lineColor(i, 0.22);
				ctx.fill();
				ctx.lineWidth = isHover ? 2 : 1.2;
				ctx.strokeStyle = isHover ? '#f59e0b' : lineColor(i, 0.9);
				ctx.stroke();
			}

			if (showText && line.text) {
				const pad = b.h * 0.15;
				let fontSize = b.h * 0.75;
				ctx.font = `500 ${fontSize}px system-ui, sans-serif`;
				let textW = ctx.measureText(line.text).width;
				if (textW + pad * 2 > b.w) {
					fontSize *= (b.w - pad * 2) / textW;
					ctx.font = `500 ${fontSize}px system-ui, sans-serif`;
					textW = ctx.measureText(line.text).width;
				}
				const dimmed = hoveredLine >= 0 && !isHover;
				const bgW = Math.max(b.w, textW + pad * 2);
				ctx.fillStyle = dimmed ? 'rgba(0, 0, 0, 0.25)' : 'rgba(0, 0, 0, 0.78)';
				ctx.fillRect(b.x, b.y, bgW, b.h);
				ctx.textAlign = 'left';
				ctx.textBaseline = 'middle';
				ctx.fillStyle = dimmed ? 'rgba(255, 255, 255, 0.2)' : '#ffffff';
				ctx.fillText(line.text, b.x + pad, b.y + b.h * 0.55);
			}
		}
		ctx.restore();
	}

	function onCanvasMove(e: PointerEvent) {
		if (!alto || !controller) return;
		const { x, y } = controller.canvasToImage(e.offsetX, e.offsetY);
		let hit = -1;
		for (let i = 0; i < alto.lines.length; i++) {
			const b = alto.lines[i].bbox;
			if (x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h) {
				hit = i;
				break;
			}
		}
		if (hit !== hoveredLine) hoveredLine = hit;
	}

	function focusLine(line: Line) {
		controller?.fitToRect(line.bbox.x, line.bbox.y, line.bbox.w, line.bbox.h);
		controller?.render();
	}

	function fit() {
		controller?.fitToCanvas();
		controller?.render();
	}

	function onKey(e: KeyboardEvent) {
		const target = e.target as HTMLElement;
		if (target?.matches('input, textarea')) return;
		if ((e.key === 'ArrowLeft' || e.key === 'h' || e.key === 'k') && prevPage) {
			goto(`/viewer/${encodeURIComponent(volume)}/${encodeURIComponent(prevPage.key)}`, {
				replaceState: false,
				noScroll: true,
			});
		} else if (
			(e.key === 'ArrowRight' || e.key === 'l' || e.key === 'j' || e.key === ' ') &&
			nextPage
		) {
			e.preventDefault();
			goto(`/viewer/${encodeURIComponent(volume)}/${encodeURIComponent(nextPage.key)}`, {
				replaceState: false,
				noScroll: true,
			});
		} else if (e.key === 'f') {
			fit();
		} else if (e.key === 't') {
			showText = !showText;
		} else if (e.key === 'b') {
			showBoxes = !showBoxes;
		} else if (e.key === 'g') {
			showPolygons = !showPolygons;
		} else if (e.key === 'p') {
			showPanel = !showPanel;
		}
	}
</script>

<svelte:window onkeydown={onKey} />

<RayShell title={volume} flush>
	{#snippet center()}
		<span class="font-mono text-xs">
			<span class="text-[oklch(0.85_0.005_260)]">{pageKey.split('/').pop()}</span>
		</span>
		{#if altoError}
			<Badge variant="destructive">ALTO error</Badge>
		{:else if !alto}
			<Badge variant="secondary">no ALTO</Badge>
		{:else}
			<Badge variant="outline" class="border-white/15 text-[oklch(0.85_0.005_260)]">
				{alto.lines.length} lines · {alto.pageConfidence.toFixed(2)}
			</Badge>
		{/if}
	{/snippet}

	{#snippet actions()}
		<div class="flex items-center gap-0.5 rounded-md border border-white/10 bg-white/5 p-0.5">
			<Button
				variant="ghost"
				size="icon-sm"
				class={`text-[oklch(0.85_0.005_260)] hover:bg-white/10 hover:text-white ${showBoxes ? 'bg-white/15 text-white' : ''}`}
				onclick={() => (showBoxes = !showBoxes)}
				title="Boxes (b)"
			>
				<BoxSelect class="h-4 w-4" />
			</Button>
			<Button
				variant="ghost"
				size="icon-sm"
				class={`text-[oklch(0.85_0.005_260)] hover:bg-white/10 hover:text-white ${showPolygons ? 'bg-white/15 text-white' : ''}`}
				onclick={() => (showPolygons = !showPolygons)}
				title="Polygons (g)"
			>
				<Spline class="h-4 w-4" />
			</Button>
			<Button
				variant="ghost"
				size="icon-sm"
				class={`text-[oklch(0.85_0.005_260)] hover:bg-white/10 hover:text-white ${showText ? 'bg-white/15 text-white' : ''}`}
				onclick={() => (showText = !showText)}
				title="Text overlay (t)"
			>
				<Type class="h-4 w-4" />
			</Button>
		</div>
	{/snippet}

	{#snippet rightActions()}
		<Button
			variant="ghost"
			size="icon-sm"
			class={`text-[oklch(0.85_0.005_260)] hover:bg-white/10 hover:text-white ${showPanel ? 'bg-white/15 text-white' : ''}`}
			onclick={() => (showPanel = !showPanel)}
			title="Side panel (p)"
		>
			{#if showPanel}
				<PanelRightClose class="h-4 w-4" />
			{:else}
				<PanelRightOpen class="h-4 w-4" />
			{/if}
		</Button>
	{/snippet}

	<!-- Canvas pane -->
	<div class="bg-muted/40 relative flex-1">
		<canvas bind:this={canvasEl} class="h-full w-full" style:filter={filterCss}></canvas>

		<!-- Top-right: view controls (fit, filter) -->
		<div class="pointer-events-none absolute top-3 right-3">
			<div
				class="bg-card/90 pointer-events-auto flex items-center gap-0.5 rounded-md border p-0.5 shadow-sm backdrop-blur"
			>
				<Button variant="ghost" size="icon-sm" onclick={fit} title="Fit (f)">
					<Maximize class="h-4 w-4" />
				</Button>
				<Button
					variant={imgFilter !== 'none' ? 'secondary' : 'ghost'}
					size="icon-sm"
					onclick={() =>
						(imgFilter =
							imgFilter === 'none'
								? 'highContrast'
								: imgFilter === 'highContrast'
									? 'invert'
									: 'none')}
					title="Image filter ({imgFilter})"
				>
					<SunMedium class="h-4 w-4" />
				</Button>
			</div>
		</div>

		<!-- Bottom-center: page navigation -->
		<div class="pointer-events-none absolute bottom-3 left-1/2 flex -translate-x-1/2 items-center">
			<div
				class="bg-card/90 pointer-events-auto flex items-center rounded-md border shadow-sm backdrop-blur"
			>
				<Button
					variant="ghost"
					size="icon-sm"
					class="rounded-r-none"
					disabled={!prevPage}
					onclick={() =>
						prevPage &&
						goto(`/viewer/${encodeURIComponent(volume)}/${encodeURIComponent(prevPage.key)}`, {
							noScroll: true,
						})}
					title="Previous (←)"
				>
					<ChevronLeft class="h-4 w-4" />
				</Button>
				<span
					class="text-muted-foreground min-w-[64px] border-x px-2 text-center font-mono text-xs tabular-nums"
				>
					{idx >= 0 ? idx + 1 : '?'} / {pages.length}
				</span>
				<Button
					variant="ghost"
					size="icon-sm"
					class="rounded-l-none"
					disabled={!nextPage}
					onclick={() =>
						nextPage &&
						goto(`/viewer/${encodeURIComponent(volume)}/${encodeURIComponent(nextPage.key)}`, {
							noScroll: true,
						})}
					title="Next (→)"
				>
					<ChevronRight class="h-4 w-4" />
				</Button>
			</div>
		</div>
	</div>

	<!-- Right pane: catalog metadata + ALTO lines.
	     The pane is shown whenever ALTO is loaded (today's behaviour) OR the
	     user has the panel toggled on AND we have catalog metadata. That way
	     batches without ALTO yet still show their archival context. -->
	{#if showPanel && (alto || catalog)}
		<aside class="bg-card flex w-96 shrink-0 flex-col overflow-hidden border-l">
			{#if catalog}
				<!-- EAD metadata for the current batch. fonds_title › series_title is
				     usually the most useful framing; volume_title + date_text answers
				     "what year is this", description fills in the human note (e.g.
				     "jan-sept, supplement"). The bildvisning link lets the user
				     compare against Riksarkivet's own viewer. -->
				<div class="bg-muted/40 border-b px-3 py-2 text-xs">
					<div
						class="text-muted-foreground flex items-center justify-between text-[11px] font-medium tracking-wide uppercase"
					>
						<span>Volume</span>
						{#if catalog.bildvisning_url}
							<a
								href={catalog.bildvisning_url}
								target="_blank"
								rel="noopener"
								class="text-primary font-normal normal-case hover:underline"
							>
								Riksarkivet ↗
							</a>
						{/if}
					</div>
					<div class="text-foreground mt-1 text-sm leading-snug font-medium">
						{catalog.fonds_title || '(untitled fonds)'}
					</div>
					{#if catalog.series_title && catalog.series_title !== catalog.fonds_title}
						<div class="text-muted-foreground text-xs">{catalog.series_title}</div>
					{/if}
					<div class="mt-1 flex flex-wrap items-center gap-1.5">
						{#if catalog.volume_title}
							<span class="font-mono text-xs">{catalog.volume_title}</span>
						{/if}
						{#if catalog.date_text && catalog.date_text !== catalog.volume_title}
							<span class="text-muted-foreground text-xs">·</span>
							<span class="text-muted-foreground text-xs">{catalog.date_text}</span>
						{/if}
					</div>
					{#if catalog.description}
						<div class="text-muted-foreground mt-1.5 text-xs">{catalog.description}</div>
					{/if}
					<div class="mt-1.5 flex items-center gap-2 text-[11px]">
						<Badge variant="secondary" class="text-[10px]">{catalog.archive_code}</Badge>
						<span class="text-muted-foreground font-mono">{catalog.reference_code}</span>
					</div>
				</div>
			{/if}

			{#if alto || xmlText}
				<div
					class="text-muted-foreground flex items-center justify-between gap-2 border-b px-3 py-2 text-[11px] font-medium tracking-wide uppercase"
				>
					<div class="bg-muted/40 flex items-center gap-0.5 rounded-md border p-0.5">
						<button
							type="button"
							onclick={() => (view = 'lines')}
							class={`rounded px-2 py-0.5 text-[11px] tracking-wide uppercase transition
								${view === 'lines' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}
						>
							Lines
						</button>
						<button
							type="button"
							onclick={() => (view = 'xml')}
							class={`rounded px-2 py-0.5 text-[11px] tracking-wide uppercase transition
								${view === 'xml' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}
						>
							XML
						</button>
					</div>
					{#if view === 'lines' && alto}
						<span class="font-mono lowercase">
							{alto.lines.length} · conf {alto.pageConfidence.toFixed(2)}
						</span>
					{:else if view === 'xml' && xmlText}
						<button
							type="button"
							onclick={copyXml}
							class="text-muted-foreground hover:bg-muted hover:text-foreground rounded border px-2 py-0.5 text-[11px] tracking-wide uppercase transition"
						>
							{copied ? 'Copied' : 'Copy'}
						</button>
					{/if}
				</div>
				{#if view === 'lines'}
					{#if alto}
						<div class="flex-1 overflow-y-auto">
							{#each alto.lines as line, i (i)}
								<button
									type="button"
									onmouseenter={() => (hoveredLine = i)}
									onmouseleave={() => (hoveredLine = -1)}
									onclick={() => focusLine(line)}
									class={`border-border/40 block w-full border-b px-3 py-2 text-left text-sm transition
										${i === hoveredLine ? 'text-foreground bg-amber-500/15' : 'hover:bg-muted/50'}`}
								>
									<span>{line.text || '∅'}</span>
									<span class="text-muted-foreground ml-2 font-mono text-[10px]">
										{line.confidence.toFixed(2)}
									</span>
								</button>
							{/each}
						</div>
					{:else}
						<div class="text-muted-foreground flex-1 px-3 py-4 text-xs">
							No ALTO available for this page.
						</div>
					{/if}
				{:else if view === 'xml'}
					{#if xmlText}
						<pre
							class="flex-1 overflow-auto px-3 py-2 font-mono text-[11px] leading-snug break-words whitespace-pre-wrap">{xmlText}</pre>
					{:else}
						<div class="text-muted-foreground flex-1 px-3 py-4 text-xs">
							No ALTO available for this page.
						</div>
					{/if}
				{/if}
			{/if}
		</aside>
	{/if}
</RayShell>
