<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import {
		type Hit,
		type DocChunk,
		type DiarTurn,
		activeView,
		viewForHit,
		getDocChunks,
		getDiarization,
		mediaUrl,
	} from '@rask/explorer-api';
	import { voiceSearch } from '$lib/voice-search.svelte';
	import { fmtTime, hitKey } from '$lib/utils';
	import { ChevronRight, Maximize2, Minimize2 } from '@lucide/svelte';
	import { Button } from '@rask/ui';
	import ChunkBodyWindow from './chunk-body-window.svelte';
	import { panesFor, shownTab } from '$lib/pane-capabilities';
	import ChunkTimeline from './chunk-timeline.svelte';
	import DiarizationTimeline from './diarization-timeline.svelte';
	import { on } from 'svelte/events';

	type Props = {
		hit: Hit | null;
		query?: string;
	};
	let { hit, query = '' }: Props = $props();

	// Sliding transcript window: how many chunks of context to show on each side
	// of the currently-playing chunk. Tunable; the window stays ≤ BEFORE+AFTER+1.
	const WINDOW_BEFORE = 1;
	const WINDOW_AFTER = 1;

	// Which element this corpus actually needs — read from the dataset's own declaration, so a
	// page-image corpus never gets a <video>. Derived, not constant: the dataset can be switched
	// under the pane.
	/** The view THIS hit is rendered through, not the active corpus's.
	 *
	 *  Every accessor below was `activeView()`, which is correct exactly while one corpus fills the
	 *  screen. In a fused result set carrying rows from two corpora it describes every row as if it
	 *  came from whichever corpus happens to be selected — and it renders, so nothing complains. The
	 *  hit names its own origin (`_dataset`, stamped by the search service); this reads it.
	 *
	 *  Falls back to the active view for a hit that names none, which reproduces the previous
	 *  behaviour exactly for the case where that behaviour was right. */
	const view = $derived(hit ? viewForHit(hit) : activeView());

	const mediaKind = $derived(view.mediaKind);

	// WHICH PANES this corpus may show. The media element was already chosen from the declared mime,
	// but the chrome around it was not gated at all — so an image corpus got a time scrubber and both
	// audio tabs. Same rule as the zone sidebar: gate on what the corpus DECLARES.
	const panes = $derived(panesFor(view));

	let mediaEl = $state<HTMLVideoElement | HTMLAudioElement | null>(null);
	let mediaError = $state<string | null>(null);

	// Whole-doc time + chunk envelope drive the timeline under the video. Svelte
	// polls currentTime via its own rAF (only while playing) through the
	// `bind:currentTime` on <video> — smooth, auto-cleaned, zero manual loop.
	let currentTime = $state(0);
	let duration = $state(0);
	let docChunks = $state<DocChunk[]>([]);

	// Bridge-bar tab: which sync view is shown under the video. Default
	// 'transcript' so existing behaviour (the karaoke window) is unchanged; the
	// <video> stays mounted across tabs so playback/seek never resets.
	let tab = $state<'transcript' | 'speakers'>('transcript');

	/** The tab actually shown. DERIVED, not a corrected `tab`: clamping by assigning inside an effect
	 *  hides the coupling, and it would rewrite the user's pick where they cannot see it happen. */
	const shown = $derived(shownTab(tab, panes));
	let diarTurns = $state<DiarTurn[]>([]);

	// Search hits ship `alignments: []` (the per-word timing blob is ~80% of a
	// search payload and only the player needs it). On opening a hit we lazy-fetch
	// the WHOLE document transcript (keyed on doc_id) so a sliding window of chunks
	// around the playhead can render past the selected chunk; atlas/selection hits
	// carry only the selected chunk so we can't rely on `hit.alignments`. The seek
	// $effect below sets currentTime to the hit's start, and the windowed transcript
	// tracks it once these absolute-time chunks populate. `docChunks` is the single
	// source — we no longer flatten into one giant alignments array.
	$effect(() => {
		const h = hit;
		if (!h) {
			docChunks = [];
			return;
		}
		docChunks = [];
		let cancelled = false; // supersede guard + leak guard
		getDocChunks(viewForHit(h).docId(h))
			.then((doc) => {
				if (!cancelled) docChunks = doc.chunks;
			})
			.catch(() => {
				/* leave empty — the video still plays, just no karaoke */
			});
		return () => {
			cancelled = true;
		};
	});

	// Lazy-fetch speaker turns for the Speakers tab, mirroring the transcript
	// fetch above. Keyed on doc_id; `built: false` (table/doc absent) yields an
	// empty list and the tab shows a muted "not built" note. The <video> plays
	// regardless — this is purely the timeline overlay.
	$effect(() => {
		const h = hit;
		if (!h) {
			diarTurns = [];
			return;
		}
		diarTurns = [];
		let cancelled = false; // supersede guard + leak guard
		getDiarization(viewForHit(h).docId(h))
			.then((d) => {
				if (!cancelled) diarTurns = d.turns;
			})
			.catch(() => {
				/* leave empty — the video still plays, just no speaker timeline */
			});
		return () => {
			cancelled = true;
		};
	});

	// Gate for the timeline's "Find this voice" buttons: the shared voiceSearch
	// status probe (idempotent, never throws — `built` stays false on failure so
	// the affordance simply never shows). onMount, not init, so a prerender
	// never fetches.
	onMount(() => {
		voiceSearch.probe();
	});

	// "Find this voice" → queue the request on the shared voiceSearch store,
	// then navigate to the Search page, which consumes `pending` in an $effect.
	// A store + goto(home) — NOT a `/?voice_*` URL — because when this pane is
	// already hosted on the Search page a same-route goto() does not remount it, and
	// +page.svelte reads those params in onMount only; the store path works from
	// every PlayerPane host (search, tree panel, atlas overlay, workflow
	// inspector). External `/?voice_doc=…` deep-links still work on fresh loads.
	const findVoice = (pick: { speaker: string } | { turnId: number }) => {
		const h = hit;
		if (!h) return;
		const docId = view.docId(h);
		// Chip label: the descriptor's display title for this row (mirrors hit-card).
		const label = view.title(h) || undefined;
		voiceSearch.request(
			'speaker' in pick ? { docId, speaker: pick.speaker } : { docId, turnId: pick.turnId },
			label,
		);
		void goto(`${base}/`);
	};

	// Which chunk the playhead is in. Reactive on currentTime + docChunks + hit.
	// 1) time-based: the chunk whose [start,end) contains currentTime.
	// 2) once playing but in an inter-chunk gap (silence between speech segments):
	//    clamp to the nearest PRECEDING chunk so the window stays at the playhead.
	//    This must beat the hit fallback — otherwise the window snaps back to the
	//    opened hit's chunk on every silence gap after the user has played past it.
	// 3) before playback (t === 0, pre-seek): fall back to the opened hit's segment
	//    so the right chunk is shown the instant a hit opens; else 0.
	const currentChunkIdx = $derived.by((): number => {
		const cs = docChunks;
		if (cs.length === 0) return 0;
		const t = currentTime;
		const i = cs.findIndex((c) => {
			const span = view.time(c);
			return span != null && t >= span.start && t < span.end;
		});
		if (i !== -1) return i;
		if (t > 0) {
			let last = 0;
			for (let k = 0; k < cs.length; k++) {
				const span = view.time(cs[k]!);
				if (span != null && span.start <= t) last = k;
			}
			return last;
		}
		const h = hit;
		if (h) {
			const hk = hitKey(h);
			const j = cs.findIndex((c) => hitKey(c) === hk);
			if (j !== -1) return j;
		}
		return 0;
	});

	// The slice `lo` — the absolute index of the first windowed chunk. The window
	// child uses it to flag the playing block without re-deriving the window.
	const windowStartIdx = $derived(Math.max(0, currentChunkIdx - WINDOW_BEFORE));

	// Window of chunks around the playhead. As currentTime crosses into the next
	// chunk, currentChunkIdx increments → this slice shifts by one (prev drops,
	// next appears): the recycle behaviour, with zero manual rotation.
	const windowChunks = $derived.by((): DocChunk[] => {
		const cs = docChunks;
		if (cs.length === 0) return [];
		const lo = windowStartIdx;
		const hi = Math.min(cs.length, currentChunkIdx + WINDOW_AFTER + 1);
		return cs.slice(lo, hi);
	});

	// The descriptor row key of the opened hit → highlights its segment.
	const activeKey = $derived(hit ? hitKey(hit) : null);

	// The playing chunk's [start,end] → feeds the diarization timeline's "Chunk"
	// zoom. Falls back to the opened hit's span before the doc transcript loads.
	const currentChunk = $derived(docChunks[currentChunkIdx]);
	const chunkStart = $derived.by((): number => {
		const c = currentChunk ?? hit;
		return c ? (viewForHit(c).time(c)?.start ?? 0) : 0;
	});
	const chunkEnd = $derived.by((): number => {
		const c = currentChunk ?? hit;
		return c ? (viewForHit(c).time(c)?.end ?? 0) : 0;
	});

	// Timeline click → jump there and play. Mirrors the hit-seek $effect; here
	// the doc is already loaded so we seek immediately.
	const seekTo = (t: number) => {
		if (!mediaEl) return;
		try {
			mediaEl.currentTime = t;
		} catch {
			// ignore — metadata may not be ready; the bound currentTime corrects it
		}
		mediaEl.play().catch(() => {});
	};

	// The unified video+transcript card. We fullscreen THIS wrapper (not the bare
	// <video>) so the live transcript overlay survives — native video fullscreen
	// can't render HTML on top.
	let fsWrap = $state<HTMLDivElement | null>(null);
	let isFullscreen = $state(false);

	const toggleFullscreen = () => {
		if (document.fullscreenElement) {
			void document.exitFullscreen();
		} else {
			void fsWrap?.requestFullscreen();
		}
	};

	// Mirror the browser's fullscreen state into a rune so the layout can react.
	$effect(() => {
		const onChange = () => {
			isFullscreen = document.fullscreenElement === fsWrap;
		};
		const offFullscreen = on(document, 'fullscreenchange', onChange);
		return () => {
			offFullscreen();
		};
	});

	// Archival metadata, collapsed by default into an accordion so it doesn't eat
	// the height the transcript needs — expand on demand.
	let showMeta = $state(false);

	// Archival metadata shown under the player — descriptor-declared fields plus
	// caption + time span, only the ones present (absent values already dropped).
	const metaRows = $derived.by((): [string, string][] => {
		const h = hit;
		if (!h) return [];
		const rows: [string, string][] = [];
		const caption = view.caption(h);
		if (caption) rows.push(['Caption', caption]);
		const t = view.time(h);
		if (t) rows.push(['Time', `${fmtTime(t.start)} → ${fmtTime(t.end)}`]);
		const dur = view.duration(h);
		if (dur != null) rows.push(['Length', fmtTime(dur)]);
		for (const m of view.metadata(h)) rows.push([m.label, m.value]);
		return rows;
	});

	/**
	 * Whenever `hit` changes, seek the player to the hit's start and play.
	 *
	 * `src` is owned reactively by the `<video src={mediaUrl(hit)}>` binding
	 * below — this effect must NOT touch it. The old code called
	 * `el.removeAttribute('src'); el.load()` in cleanup; when the next hit was
	 * in the *same* document the bound URL didn't change, so Svelte never
	 * re-applied `src` and the element was left sourceless and wedged (the
	 * "second click freezes the player until full refresh" bug).
	 *
	 * We read the hit's doc id + start so the effect re-runs on either a new
	 * document (src changes → metadata reloads → seek on `loadedmetadata`) or a
	 * new chunk in the same already-loaded document (seek immediately, since
	 * `loadedmetadata`/`canplay` won't fire again).
	 */
	$effect(() => {
		mediaError = null;
		if (!hit || !mediaEl) return;

		const el = mediaEl;
		const start = view.time(hit)?.start ?? 0;
		let cancelled = false;
		const seek = () => {
			if (cancelled) return;
			try {
				el.currentTime = start;
			} catch {
				// ignore — some browsers throw if metadata isn't fully ready
			}
			el.play().catch(() => {});
		};
		const onError = () => {
			const err = el.error;
			const codes: Record<number, string> = {
				1: 'MEDIA_ERR_ABORTED',
				2: 'MEDIA_ERR_NETWORK',
				3: 'MEDIA_ERR_DECODE',
				4: 'MEDIA_ERR_SRC_NOT_SUPPORTED',
			};
			mediaError = err ? (codes[err.code] ?? 'unknown') : 'unknown';
		};

		// `on()` returns its own unsubscribe, so the teardown cannot drift out of sync with the
		// registration the way a hand-paired add/removeEventListener can (and `{ once: true }` handlers
		// were being removed by hand even after they had already fired).
		const offs: (() => void)[] = [];
		if (el.readyState >= 1 /* HAVE_METADATA */) {
			seek();
		} else {
			offs.push(on(el, 'loadedmetadata', seek, { once: true }));
			offs.push(on(el, 'canplay', seek, { once: true }));
		}
		offs.push(on(el, 'error', onError));

		return () => {
			cancelled = true;
			for (const off of offs) off();
			el.pause();
		};
	});
</script>

<div class="flex h-full min-h-0 flex-col gap-3 p-4">
	{#if !hit}
		<div class="text-muted-foreground m-auto text-sm">Click a hit to play.</div>
	{:else}
		<!-- ONE media component: the video and its synced transcript live in a
         single card, bridged by a slim "Transcript" bar that also carries the
         fullscreen toggle (off the video, so nothing covers it). In fullscreen
         we fullscreen THIS wrapper so the transcript can overlay the video as a
         centred caption band. -->
		{#snippet fsToggle(cls: string)}
			<button
				type="button"
				onclick={toggleFullscreen}
				title={isFullscreen ? 'Exit fullscreen' : 'Fullscreen with transcript'}
				aria-label={isFullscreen ? 'Exit fullscreen' : 'Fullscreen with transcript'}
				class={cls}
			>
				{#if isFullscreen}
					<Minimize2 class="size-4" />
				{:else}
					<Maximize2 class="size-4" />
				{/if}
			</button>
		{/snippet}

		<div
			bind:this={fsWrap}
			class={isFullscreen
	? 'relative flex min-h-0 flex-col bg-black'
	: 'border-border bg-card relative flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border shadow-sm'}
		>
			{#if isFullscreen}
				{@render fsToggle(
					'absolute right-2 top-2 z-10 rounded-md bg-black/50 p-1.5 text-white/90 backdrop-blur transition-colors hover:bg-black/70 hover:text-white',
				)}
			{/if}

			<!-- The element comes from the corpus's OWN declaration, not from an assumption. A page-
           image corpus declares `document.mime: image/png` and no `time` binding; handing that
           to a <video> is how "Video failed to load: MEDIA_ERR_SRC_NOT_SUPPORTED" happened on a
           dataset that has no video at all. See DescriptorView.mediaKind. -->
			{#if mediaKind === 'image'}
				<!-- The per-row FRAME, not the doc blob: on a paged corpus the row IS the page, so
             /api/chunk-frame/<identity> is the thing the hit points at. -->
				<img
					src={view.frameUrl(hit)}
					alt={view.title(hit)}
					class={isFullscreen
	? 'min-h-0 w-full flex-1 bg-black object-contain'
	: 'max-h-[45vh] w-full shrink-0 bg-black object-contain'}
				/>
			{:else if mediaKind === 'audio'}
				<!-- Audio keeps the transport (the timeline below is meaningful) but not a 16:9 box. -->
				<audio
					bind:this={mediaEl}
					bind:currentTime
					bind:duration
					controls
					preload="metadata"
					src={mediaUrl(hit)}
					class="w-full shrink-0 bg-black"
				></audio>
			{:else}
				<!-- 16:9 box sized from the pane WIDTH (not height) so the video stays
             visible even when the pane is dragged short. In fullscreen it fills
             the screen height instead. -->
				<video
					bind:this={mediaEl}
					bind:currentTime
					bind:duration
					controls
					controlslist="nofullscreen"
					preload="metadata"
					src={mediaUrl(hit)}
					class={isFullscreen
	? 'min-h-0 w-full flex-1 bg-black object-contain'
	: 'aspect-video max-h-[45vh] w-full shrink-0 bg-black object-contain'}
				>
					<track kind="captions" />
				</video>
			{/if}

			<!-- The scrubber is meaningless without a time axis: a still image has nothing to scrub. -->
			{#if !isFullscreen && panes.timeline}
				<ChunkTimeline
					chunks={docChunks}
					{duration}
					{currentTime}
					{activeKey}
					{currentChunkIdx}
					onSeek={seekTo}
				/>
			{/if}
			<!-- Bridge bar: ties the media to its synced views and holds the fullscreen toggle (kept off
           the media so nothing covers it). Rendered for EVERY corpus, because the toggle applies to
           an image as much as to a video — it is the TABS inside it that are conditional. Gating the
           whole bar on the timeline is what would take fullscreen away from image corpora. -->
			{#if !isFullscreen}
				<div
					class="border-border/70 text-muted-foreground flex items-center gap-1.5 border-t px-3 py-1.5 text-xs font-medium"
					data-testid="viewer-bridge-bar"
				>
					{#if panes.transcript}
						<Button
							variant={shown === 'transcript' ? 'secondary' : 'ghost'}
							size="xs"
							aria-pressed={shown === 'transcript'}
							data-testid="tab-transcript"
							onclick={() => (tab = 'transcript')}
						>
							Transcript
						</Button>
					{/if}
					{#if panes.speakers}
						<Button
							variant={shown === 'speakers' ? 'secondary' : 'ghost'}
							size="xs"
							aria-pressed={shown === 'speakers'}
							data-testid="tab-speakers"
							onclick={() => (tab = 'speakers')}
						>
							Speakers
						</Button>
					{/if}
					{@render fsToggle(
						'ml-auto rounded-md p-1 transition-colors hover:bg-muted hover:text-foreground',
					)}
				</div>
			{/if}

			<!-- Sync body. Normal: scrolls below the bar inside the card with a
           guaranteed min-height floor so it never collapses to 0 in a short pane
           (the map view's right column is wide-but-short, which used to squeeze
           the flex-1 transcript to nothing). Fullscreen: a centred caption band
           lifted ABOVE the native control bar (bottom-20) so it never covers the
           play button — only the ≤3 windowed chunks render, so it stays small.
           Fullscreen always shows the transcript overlay (the tab switch lives
           on the bridge bar, which is hidden in fullscreen). -->
			<!-- `shown` is null when the corpus declares NEITHER an alignment table nor speaker turns —
           an image corpus — so the whole sync body is absent rather than an empty panel that reads
           as a failed load. -->
			{#if shown !== null}
				<div
					class={isFullscreen
	? 'absolute inset-x-0 bottom-20 mx-auto max-h-[32%] w-[min(92%,60rem)] overflow-y-auto rounded-xl bg-black/55 px-6 py-4 text-lg leading-8 text-white backdrop-blur-sm'
	: shown === 'speakers'
		? 'flex min-h-[10rem] flex-1 flex-col overflow-hidden'
		: 'min-h-[8rem] flex-1 overflow-y-auto text-sm leading-7'}
					data-testid="viewer-sync-body"
				>
					{#if !isFullscreen && shown === 'speakers'}
						{#if diarTurns.length > 0}
							<!-- Compact speaker lanes (Chunk⇄Video zoom) pinned on top; the karaoke
                 transcript fills the rest and is the only part that scrolls, so the
                 video + lanes + transcript all stay in view. Both synced to <video>. -->
							<div class="flex h-full min-h-0 flex-col">
								<div class="shrink-0">
									<DiarizationTimeline
										turns={diarTurns}
										{currentTime}
										{duration}
										{chunkStart}
										{chunkEnd}
										onSeek={seekTo}
										voiceEnabled={voiceSearch.built}
										onFindVoice={findVoice}
									/>
								</div>
								<div
									class="border-border/70 min-h-0 flex-1 overflow-y-auto border-t text-sm leading-7"
								>
									<ChunkBodyWindow
										chunks={windowChunks}
										{currentChunkIdx}
										{windowStartIdx}
										media={mediaEl}
										{query}
										variant="panel"
									/>
								</div>
							</div>
						{:else}
							<div class="text-muted-foreground p-3 text-xs">Diarization not built for this video.</div>
						{/if}
					{:else}
						<ChunkBodyWindow
							chunks={windowChunks}
							{currentChunkIdx}
							{windowStartIdx}
							media={mediaEl}
							{query}
							variant={isFullscreen ? 'overlay' : 'panel'}
						/>
					{/if}
				</div>
			{/if}
		</div>

		{#if mediaError}
			<div class="text-destructive shrink-0 text-sm">
				Video failed to load: {mediaError}. Check
				<code>{mediaUrl(hit)}</code>.
			</div>
		{/if}

		{#if metaRows.length}
			<div class="border-border bg-card/40 shrink-0 overflow-hidden rounded-md border text-xs">
				<button
					type="button"
					onclick={() => (showMeta = !showMeta)}
					aria-expanded={showMeta}
					class="text-muted-foreground hover:bg-muted/60 flex w-full items-center gap-1.5 px-3 py-1.5 transition-colors"
				>
					<ChevronRight class="size-3.5 transition-transform {showMeta ? 'rotate-90' : ''}" />
					<span class="font-medium">Metadata</span>
					<span class="text-muted-foreground/70 ml-auto text-[0.7rem]">{metaRows.length} fields</span>
				</button>
				{#if showMeta}
					<dl class="border-border grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 border-t px-3 py-2">
						{#each metaRows as [label, value] (label)}
							<dt class="text-muted-foreground">{label}</dt>
							<dd class="text-foreground truncate font-medium" title={value}>{value}</dd>
						{/each}
					</dl>
				{/if}
			</div>
		{/if}
	{/if}
</div>
