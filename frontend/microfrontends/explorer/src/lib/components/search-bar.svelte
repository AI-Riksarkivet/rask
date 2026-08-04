<script lang="ts">
	// Chrome on the estate design system: @rask/ui Button + Select (the same primitives
	// the data/admin zones use). Input stays on @rask/ui — @rask/ui does not export one.
	import { Button } from '@rask/ui';
	import { Select } from '@rask/ui/select';
	import { Input } from '@rask/ui/input';
	import type { SelectOption } from '@rask/ui/select';
	import FilterPopover from './filter-popover.svelte';
	import HelpPopover from './help-popover.svelte';
	import SearchSettings from './search-settings.svelte';
	import { activeView, type SearchSpec, type SearchMode } from '@rask/explorer-api';
	import { untrack } from 'svelte';
	import { voiceSearch } from '$lib/voice-search.svelte';
	import { serviceHealth } from '$lib/service-health.svelte';
	import { AudioLines, Loader2, Paperclip, Search, X, ImagePlus } from '@lucide/svelte';
	import { on } from 'svelte/events';

	type Props = {
		spec: SearchSpec;
		onsubmit?: (spec: SearchSpec) => void;
		/** Bump when the spec was replaced EXTERNALLY (SavedViews apply, voice flows):
		 *  the bar re-adopts the spec into its dials. Echoes of the bar's own submit
		 *  must NOT bump — adoptSpec is a lossy inverse of buildSpec, so adopting an
		 *  echo would flip custom vector-space/image modes and reset latent dials. */
		adoptSignal?: number;
	};
	let { spec = $bindable(), onsubmit, adoptSignal = 0 }: Props = $props();

	/**
	 * `kind` (Keyword | Vector | Hybrid) maps to the backend mode; `style`
	 * (loose | phrase | fuzzy) only matters when keyword is involved. Both are
	 * plain strings so they bind cleanly to the shadcn Select/RadioGroup.
	 * Internal values stay 'meaning'/'both' to map onto modes semantic/hybrid.
	 */
	let kind = $state<string>('keyword');
	// For the Scene kind: search the caption field by meaning (vector) or keyword (BM25).
	let sceneMethod = $state<string>('vector');
	let style = $state<string>('loose');
	let rerank = $state(false);
	let rerankN = $state('20');
	let resultN = $state('100');
	// Hybrid blend: 0 = pure FTS, 100 = pure vector; null = parameter-free RRF.
	let weightPct = $state<number | null>(null);
	let q = $state('');
	// Optional separate text for the VECTOR leg of hybrid; falls back to `q`.
	let qVec = $state('');
	let imageFile = $state<File | null>(null);

	/** The ONE spec→locals mapping — at init and whenever the spec is replaced
	 *  externally (SavedViews apply, voice/atlas flows), so the bar always shows
	 *  what will actually run. Replaces the parent's old force-remount hack. */
	function adoptSpec(s: SearchSpec): void {
		kind =
			s.mode === 'semantic'
				? 'meaning'
				: s.mode === 'scene' || s.mode === 'scene_fts'
					? 'scene'
					: s.mode === 'fts'
						? 'keyword'
						: 'both';
		sceneMethod = s.mode === 'scene_fts' ? 'fts' : 'vector';
		style = s.phrase ? 'phrase' : s.fuzziness === 2 ? 'fuzzy' : 'loose';
		rerank = s.rerank ?? false;
		rerankN = String(s.rerankN ?? 20);
		resultN = String(s.n ?? 100);
		weightPct = s.weight === undefined || s.weight === null ? null : Math.round(s.weight * 100);
		q = s.q;
		qVec = s.qVec ?? '';
		imageFile = s.image ?? null;
	}
	adoptSpec(spec);

	// Resync ONLY when the parent signals an external replacement. Value-based echo
	// detection is inherently ambiguous (applying a saved view identical to the last
	// submit looks exactly like the submit's own echo), so the signal is explicit.
	// `untrack` keeps `spec` out of the effect's deps — echoes never re-run it.
	// Initial value IS the intent (baseline at mount) — untrack documents that.
	let lastSignal = untrack(() => adoptSignal);
	$effect(() => {
		if (adoptSignal !== lastSignal) {
			lastSignal = adoptSignal;
			untrack(() => adoptSpec(spec));
		}
	});

	// Title-case a raw vector-space key for a dial label ('audio' → 'Audio',
	// 'voice_print' → 'Voice Print').
	const humanize = (key: string): string =>
		key.replace(/[_-]+/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());

	// Offered search kinds come from the dataset's declared modes — a mode the
	// dataset lacks (e.g. no Scene captions, no vectors) never shows. The named
	// roles keep their friendly labels; ANY OTHER declared text-embedding space
	// (a new embedding column) appears as its own option keyed by its space name,
	// so a fresh embedding is searchable with no code change here. Image-encoder
	// spaces use the Image attach affordance instead of a dial entry.
	// A mode is offered when the DATASET declares it AND the DEPLOYMENT can serve it. Those are two
	// different questions and only the first was being asked: this estate ran with no embedding service
	// (the viewer's `embed_url` still defaulted to lance-media's sidecar-era 127.0.0.1:8001 and the chart
	// never re-pointed it), so Vector and Hybrid were listed, selectable, and 503'd — measured through the
	// ingress on 2026-07-26: `mode=fts` 200 with rows, `mode=semantic` and `mode=hybrid` 503 "embed …".
	// A user picked one, got a failure toast, and learned nothing; the only signal was a permanently red
	// dot in the sidebar corner.
	//
	// Disabled rather than hidden, on purpose. Hiding says "this dataset has no vectors", which is a
	// different and wrong story — the corpus HAS 145k vector-bearing chunks. Disabled with the reason says
	// "this exists but the encoder is down", which is the truth and is actionable.
	const encoderModes = new Set(['meaning', 'both']);
	const kindOptions = $derived.by<SelectOption[]>(() => {
		const view = activeView();
		const blocked = !serviceHealth.canEmbed;
		const opts: SelectOption[] = [];
		if (view.hasMode('fts')) opts.push({ value: 'keyword', label: 'Keyword' });
		if (view.hasMode('semantic')) opts.push({ value: 'meaning', label: 'Vector' });
		if (view.hasMode('hybrid')) opts.push({ value: 'both', label: 'Hybrid' });
		if (view.hasMode('scene')) opts.push({ value: 'scene', label: 'Scene' });
		const named = new Set(['semantic', 'visual', 'scene']);
		for (const s of view.vectorSpaces) {
			if (!named.has(s.key) && s.encoder !== 'image') {
				opts.push({ value: s.key, label: humanize(s.key) });
			}
		}
		// Every non-keyword, non-scene mode needs a query encoder; a declared embedding space does too.
		return opts.map((o) =>
			blocked && (encoderModes.has(o.value) || !['keyword', 'scene'].includes(o.value))
				? { ...o, label: `${o.label} — encoder offline`, disabled: true }
				: o,
		);
	});

	// The first declared IMAGE-embedding space (any key — not just `visual`) — an
	// image-only query runs this mode, and its presence gates the Image attach
	// affordance below. So an image embedding under any name still gets upload.
	const imageSpace = $derived(activeView().vectorSpaces.find((s) => s.encoder === 'image') ?? null);
	const hasVisual = $derived(imageSpace !== null);

	// Keep `kind` valid if the dataset doesn't offer the current mode — or if the deployment stopped being
	// able to serve it (the encoder went down while a vector mode was selected). Falls back to the first
	// SELECTABLE option, so it cannot land on a disabled one.
	$effect(() => {
		const usable = kindOptions.filter((o) => !o.disabled);
		if (usable.length > 0 && !usable.some((o) => o.value === kind)) {
			kind = usable[0]!.value;
		}
	});

	let imagePreview: string | null = $state(null);
	$effect(() => {
		if (imageFile) {
			const url = URL.createObjectURL(imageFile);
			imagePreview = url;
			return () => URL.revokeObjectURL(url);
		}
		imagePreview = null;
	});

	/** Derive backend SearchMode from the dial + image-attach state. */
	function buildSpec(): SearchSpec {
		let mode: SearchMode;
		const keywordInvolved = kind === 'keyword' || kind === 'both';
		const hasText = q.trim() !== '' || qVec.trim() !== '';
		// Image + any text → 3-way "all" (FTS + text-vector + frame-vector). Image
		// alone → visual. Otherwise the dial decides.
		if (imageFile && hasText) mode = 'all';
		else if (imageFile) mode = imageSpace?.key ?? 'visual';
		else if (kind === 'meaning') mode = 'semantic';
		else if (kind === 'scene') mode = sceneMethod === 'fts' ? 'scene_fts' : 'scene';
		else if (kind === 'both') mode = 'hybrid';
		else if (kind === 'keyword') mode = 'fts';
		else mode = kind; // a raw vector-space key — a new embedding searched by its own mode
		return {
			q: q.trim(),
			qVec: qVec.trim() || undefined,
			mode,
			phrase: keywordInvolved && style === 'phrase',
			fuzziness: keywordInvolved && style === 'fuzzy' ? 2 : 0,
			rerank,
			rerankN: rerank ? Number(rerankN) : undefined,
			weight: kind === 'both' && weightPct !== null ? weightPct / 100 : undefined,
			n: Number(resultN) || 30,
			image: imageFile,
			// Filter fields are owned by <FilterPopover> on `spec` — pass them
			// through so a new search keeps the active filters (the old buildSpec
			// dropped `where`/`prefilter`, silently discarding the SQL filter).
			filters: spec.filters,
			where: spec.where,
			prefilter: spec.prefilter,
		};
	}

	function submit() {
		const next = buildSpec();
		spec = next;
		onsubmit?.(next);
	}

	let fileInput = $state<HTMLInputElement | null>(null);

	// ── Voice clip attach ("find this voice" from an uploaded snippet) ──
	// Gated on voiceSearch.built (the page's one-shot status probe). Picking a
	// file auto-applies: requestUpload() queues it on the shared store and the
	// search page consumes it — no submit step, mirroring the image affordance.
	let audioInput = $state<HTMLInputElement | null>(null);
	/** Inline error for a rejected clip (too large) — never sent to the API. */
	let audioError = $state<string | null>(null);
	/** Mirrors the backend cap (`services/viewer/services/voice_service.py` _MAX_UPLOAD_BYTES);
	 *  bigger files would 400 server-side, so they're refused client-side. */
	const MAX_AUDIO_BYTES = 25 * 1024 * 1024;

	function pickAudio(f: File) {
		audioError = null;
		if (f.size > MAX_AUDIO_BYTES) {
			const mb = (f.size / (1024 * 1024)).toFixed(1);
			audioError = `"${f.name}" is ${mb} MB — voice clips are capped at 25 MB. Trim to a few seconds of one person speaking.`;
			return;
		}
		voiceSearch.requestUpload(f);
	}

	function dropZone(node: HTMLElement) {
		const onDragOver = (e: DragEvent) => e.preventDefault();
		const onDrop = (e: DragEvent) => {
			e.preventDefault();
			const f = e.dataTransfer?.files?.[0];
			if (hasVisual && f && f.type.startsWith('image/')) imageFile = f;
		};
		const offDragOver = on(node, 'dragover', onDragOver);
		const offDrop = on(node, 'drop', onDrop);
		return () => {
			offDragOver();
			offDrop();
		};
	}

	// Help popover examples — also reused as plain-language summary copy.
	const examples = {
		keyword: {
			label: 'Keyword',
			example: 'betänkandet',
			explain:
				'Match transcripts that CONTAIN your words (in any order). Swedish stemmer also accepts inflections — "betänkandet" finds "betänkande" / "betänkanden" / "betänkandet".',
		},
		phrase: {
			label: 'Phrase',
			example: 'alkoholmonopolets framtid',
			explain: 'Words must appear in this EXACT order, side by side.',
		},
		fuzzy: {
			label: 'Fuzzy',
			example: 'betänkadet',
			explain:
				'Like Keyword but allows up to 2 letter typos per word — useful when unsure of spelling.',
		},
		meaning: {
			label: 'Vector',
			example: 'klimatkris',
			explain:
				'Vector search — finds chunks that DISCUSS the topic, even if those exact words aren\'t there. "klimat" can find "miljö" / "ekosystem".',
		},
		both: {
			label: 'Hybrid',
			example: 'regeringens beslut',
			explain: 'Run Keyword (FTS) AND Vector together, fuse the rankings. Recommended default.',
		},
		scene: {
			label: 'Scene',
			example: 'demonstranter med plakat',
			explain:
				'Searches the AI-written Swedish caption of each video FRAME — finds clips by what is visible on screen, described in words. Complements Image search (which matches raw visuals).',
		},
	} satisfies Record<string, { label: string; example: string; explain: string }>;

	/** Placeholder for the single query box (non-hybrid modes). */
	const singlePlaceholder = $derived(
		kind === 'scene'
			? "Describe what's on screen…"
			: kind === 'meaning'
				? 'Search by meaning…'
				: 'Search transcripts…',
	);

	/** Single-line summary that always reflects the current setting. */
	const summary = $derived.by(() => {
		if (imageFile) {
			if (kind === 'meaning' || kind === 'both')
				return 'Image + your text → fused over transcript meaning AND visually similar frames.';
			return 'Image only → finds visually similar video frames. (Switch to Vector or Hybrid to also use your text.)';
		}
		if (kind === 'scene') return examples.scene.explain;
		if (kind === 'meaning') return examples.meaning.explain;
		if (kind === 'both')
			return `Hybrid — ${examples.keyword.explain} PLUS ${examples.meaning.explain}`;
		if (style === 'phrase') return examples.phrase.explain;
		if (style === 'fuzzy') return examples.fuzzy.explain;
		return examples.keyword.explain;
	});
</script>

<div class="px-6 py-3" {@attach dropZone}>
	<form
		class="flex flex-col gap-2.5"
		onsubmit={(e) => {
	e.preventDefault();
	submit();
}}
	>
		<!-- ── Row A: mode + actions ── -->
		<div class="flex flex-wrap items-center gap-2">
			<Select bind:value={kind} options={kindOptions} ariaLabel="Search mode" />
			<span class="text-muted-foreground hidden flex-1 truncate text-xs lg:inline">
				{summary}
			</span>
			<div class="ml-auto flex items-center gap-1.5">
				{#if hasVisual}
					<Button
						type="button"
						variant="outline"
						size="sm"
						title="Attach an image (drag-drop also works) — search by visual similarity"
						onclick={() => fileInput?.click()}
					>
						<Paperclip />
						Image
					</Button>
					<input
						bind:this={fileInput}
						type="file"
						accept="image/*"
						class="hidden"
						onchange={(e) => {
	const f = e.currentTarget.files?.[0];
	if (f && f.type.startsWith('image/')) imageFile = f;
	e.currentTarget.value = '';
}}
					/>
				{/if}
				{#if voiceSearch.built}
					<Button
						type="button"
						variant="outline"
						size="sm"
						disabled={voiceSearch.uploadPending}
						title="Attach a short audio clip (≤ 25 MB) — find everywhere this voice speaks"
						onclick={() => audioInput?.click()}
					>
						{#if voiceSearch.uploadPending}
							<Loader2 class="animate-spin" />
						{:else}
							<AudioLines />
						{/if}
						Voice
					</Button>
					<input
						bind:this={audioInput}
						type="file"
						accept="audio/*,video/mp4,.m4a,.mp3,.wav"
						class="hidden"
						onchange={(e) => {
	const f = e.currentTarget.files?.[0];
	e.currentTarget.value = '';
	if (f) pickAudio(f);
}}
					/>
				{/if}
				<SearchSettings
					bind:resultN
					bind:rerank
					bind:rerankN
					bind:weightPct
					bind:style
					bind:sceneMethod
					{kind}
				/>
				<FilterPopover bind:spec onchange={submit} />
				<HelpPopover
					{examples}
					onpick={(key, ex) => {
	if (key === 'phrase' || key === 'fuzzy') {
		kind = 'keyword';
		style = key;
	} else if (key === 'meaning') kind = 'meaning';
	else if (key === 'scene') kind = 'scene';
	else if (key === 'both') {
		kind = 'both';
		style = 'loose';
	} else {
		kind = 'keyword';
		style = 'loose';
	}
	q = ex;
}}
				/>
			</div>
		</div>

		<!-- ── Row B: query input(s) + Search ── -->
		<!-- Same single row in every mode (consistent height); Hybrid just adds the
         second input. Labels live in the placeholders so the row never grows. -->
		{#if kind === 'both'}
			<div class="flex items-center gap-2">
				<Input
					bind:value={q}
					type="search"
					class="h-8 rounded-lg sm:w-52"
					placeholder="Keyword — exact words"
					aria-label="Keyword (FTS)"
				/>
				<Input
					bind:value={qVec}
					type="search"
					class="h-8 flex-1 rounded-lg sm:max-w-2xl"
					placeholder="Vector — search by meaning (primary)"
					aria-label="Vector — search by meaning"
				/>
				<Button type="submit">
					<Search />
					Search
				</Button>
			</div>
		{:else}
			<div class="flex items-center gap-2">
				<Input
					bind:value={q}
					type="search"
					class="h-8 flex-1 rounded-lg"
					placeholder={singlePlaceholder}
				/>
				<Button type="submit">
					<Search />
					Search
				</Button>
			</div>
		{/if}

		{#if audioError}
			<p class="text-destructive text-xs" role="alert">{audioError}</p>
		{/if}

		{#if imagePreview && imageFile}
			<div
				class="border-border bg-muted flex w-fit items-center gap-2 rounded-lg border py-1 pr-1 pl-1"
			>
				<img src={imagePreview} alt="" class="h-12 w-auto rounded-md object-cover" />
				<span class="text-foreground max-w-[12rem] truncate text-xs" title={imageFile.name}>
					{imageFile.name}
				</span>
				<Button
					variant="ghost"
					size="icon-xs"
					title="Remove image"
					aria-label="Remove image"
					onclick={() => (imageFile = null)}
				>
					<X />
				</Button>
			</div>
		{/if}

		<!-- Summary on smaller screens (hidden when shown inline in Row A). -->
		<div class="text-muted-foreground flex items-baseline gap-2 text-xs lg:hidden">
			{#if imageFile}<ImagePlus class="text-primary size-3.5 self-center" />{/if}
			<span>{summary}</span>
		</div>
	</form>
</div>
