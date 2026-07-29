<script lang="ts">
	import { base } from '$app/paths';
	/**
	 * Guide — the in-app explainer. Two tabs: how Search works (the full,
	 * animated walk-through that used to be crammed in the `?` popover) and how
	 * the Atlas map works. Self-contained: no props, no fetching. Scroll-reveal
	 * via a tiny IntersectionObserver action; the pipeline + lanes use CSS
	 * animations so the "several searches at once, then merge" flow reads as
	 * motion. Content verified against services/search/services/service.py.
	 */
	// `Map as MapIcon` (same aliasing as routes/+page.svelte): the bare icon name
	// would shadow the global Map constructor used by rrfRows' rankOf below.
	import { Search, Map as MapIcon, Sparkles, ArrowRight } from '@lucide/svelte';

	let tab = $state<'search' | 'atlas'>('search');

	/** Add `.in-view` the first time a node scrolls into view → CSS does the rest. */
	function reveal(node: HTMLElement, opts: { delay?: number } = {}) {
		node.style.transitionDelay = `${opts.delay ?? 0}ms`;
		const io = new IntersectionObserver(
			(entries) => {
				for (const e of entries) {
					if (e.isIntersecting) {
						node.classList.add('in-view');
						io.unobserve(node);
					}
				}
			},
			{ threshold: 0.12, rootMargin: '0px 0px -6% 0px' },
		);
		io.observe(node);
		return { destroy: () => io.disconnect() };
	}

	// The four signals / "judges", in the canonical order.
	const JUDGES = [
		{
			icon: '⌨️',
			name: 'Keyword',
			mode: 'fts',
			accent: 'border-l-sky-400',
			what: 'exact words you type (FTS / BM25 on the transcript)',
			give: 'your query words',
			vs: 'transcript text (BM25). Good at exact terms, names.',
		},
		{
			icon: '💬',
			name: 'Vector',
			mode: 'semantic',
			accent: 'border-l-primary',
			what: 'what a clip is about, even in other words (text vector)',
			give: 'your text → a vector',
			vs: 'text_embedding of each transcript. Good at topics, paraphrases.',
		},
		{
			icon: '🖼️',
			name: 'Image',
			mode: 'visual',
			accent: 'border-l-amber-400',
			what: 'how much the video frame looks like your image (frame vector)',
			give: 'your image → a vector',
			vs: 'frame_embedding of each video frame. Good at visually similar scenes.',
		},
		{
			icon: '🎬',
			name: 'Scene',
			mode: 'scene',
			accent: 'border-l-emerald-400',
			what: "what's visible on screen, from each frame's Swedish caption (caption vector)",
			give: 'your text → a vector',
			vs: 'caption_embedding of each frame’s Swedish caption. Good at what’s on screen ("plakat", "snöig gata").',
		},
	];

	// Lanes for the "parallel passes" diagram (the order text reads left→right).
	const LANES = [
		{
			name: 'Keyword',
			sub: 'transcript words',
			scan: 'bg-sky-400',
			text: 'text-sky-500 dark:text-sky-300',
		},
		{ name: 'Vector', sub: 'transcript meaning', scan: 'bg-primary', text: 'text-primary' },
		{
			name: 'Image',
			sub: 'video frames',
			scan: 'bg-amber-400',
			text: 'text-amber-500 dark:text-amber-300',
		},
		{
			name: 'Scene',
			sub: 'frame captions',
			scan: 'bg-emerald-400',
			text: 'text-emerald-500 dark:text-emerald-300',
		},
	];

	// Per-mode pass count (how many independent searches each UI mode fires).
	const MODES = [
		{ mode: 'Keyword', passes: '1 pass', detail: 'BM25 word-match on the transcript', fuse: '—' },
		{ mode: 'Vector', passes: '1 pass', detail: 'meaning-match on the transcript', fuse: '—' },
		{
			mode: 'Scene',
			passes: '1 pass',
			detail: 'matches the frame’s Swedish caption (meaning or keyword)',
			fuse: '—',
		},
		{
			mode: 'Image',
			passes: '1 pass',
			detail: 'image-similarity on the video frames (no text)',
			fuse: '—',
		},
		{
			mode: 'Hybrid',
			passes: '2 passes',
			detail: 'Keyword + Vector on the transcript',
			fuse: 'RRF, or the Balance slider',
		},
		{
			mode: 'Image + text',
			passes: '≤ 4 passes',
			detail: 'Keyword + Vector + Image + Scene — the dial is ignored',
			fuse: 'equal-weight RRF',
		},
	];

	// Concrete per-signal examples (generated + fact-checked against service.py).
	const SIGNAL_EXAMPLES = [
		{
			name: 'Keyword',
			icon: '⌨️',
			accent: 'border-l-sky-400',
			q: 'Kristersson · phrase: vi inför ett exportstopp',
			finds:
				"every chunk whose transcript literally says it, ranked by BM25. Fat-fingered 'Kristerson'? Turn on Fuzzy and the typo still lands.",
			misses:
				"only the words actually spoken — 'uppsägningar' won't match a clip where the minister said 'varsel'. It never reads the frame.",
			use: 'you know the exact word, name, or quote that was said.',
		},
		{
			name: 'Vector',
			icon: '💬',
			accent: 'border-l-primary',
			q: 'fler jobb',
			finds:
				"a minister saying 'vi måste öka sysselsättningen' — no shared word, same meaning (they sit close in embedding space).",
			misses:
				"can drift to merely-adjacent topics ('tillväxt'); reads only the transcript, so an unspoken on-screen chart is Scene's job.",
			use: 'you remember the gist, not the exact words.',
		},
		{
			name: 'Scene',
			icon: '🎬',
			accent: 'border-l-emerald-400',
			q: 'snöig gata',
			finds:
				"an outdoor stakeout on a snow-covered street — even though the transcript only discusses energy policy. It matched the frame's Swedish caption, not the speech.",
			misses:
				"reads only the AI caption of what's on screen (as good as the captioner). Not face recognition — it finds the snow, not a person.",
			use: 'you remember what the clip looked like, not what was said.',
		},
		{
			name: 'Image',
			icon: '🖼️',
			accent: 'border-l-amber-400',
			q: 'attach a photo of a podium with microphones',
			finds:
				'moments that LOOK the same — ministers at similar podiums, mic-cluster stakeouts. Swap in a snowy street → outdoor winter frames.',
			misses:
				'look-alike only, NOT face / identity. Add any text and the dial is ignored — your image becomes one equal vote among up to 4 passes, never a gate.',
			use: 'you have a picture of a scene and want frames that look like it.',
		},
	];

	// RRF worked example — two judges on 5 clips (A–E). Position shown 1-based for
	// readability; the score uses 0-indexed rank to match _rrf_fuse (#1 → 1/60).
	const K = 60;
	const keywordRanked = ['C', 'A', 'B'];
	const meaningRanked = ['A', 'C', 'D'];
	const rrfRows = (() => {
		// A Map + explicit undefined check: rank 0 (= position #1) is VALID — a
		// truthiness test would drop every #1-ranked clip's contribution.
		const rankOf = (list: string[]) => new Map(list.map((c, i) => [c, i]));
		const kr = rankOf(keywordRanked);
		const mr = rankOf(meaningRanked);
		const clips = [...new Set([...keywordRanked, ...meaningRanked])];
		return clips
			.map((clip) => {
				const parts: { src: string; rank: number }[] = [];
				const k = kr.get(clip);
				if (k !== undefined) parts.push({ src: '⌨️', rank: k });
				const m = mr.get(clip);
				if (m !== undefined) parts.push({ src: '💬', rank: m });
				const score = parts.reduce((a, p) => a + 1 / (K + p.rank), 0);
				return { clip, parts, score, inBoth: parts.length === 2 };
			})
			.sort((a, b) => b.score - a.score);
	})();

	const SETTINGS = [
		{ name: 'Results to return', desc: 'How many chunks to show (N, default 100).' },
		{ name: 'Rerank top', desc: 'Head size K the cross-encoder re-reads (default 20).' },
		{
			name: 'Balance',
			desc: 'Keyword ↔ meaning weight. Hybrid only (ignored once an image is added).',
		},
		{
			name: 'Match style',
			desc: 'loose words, exact phrase, or fuzzy (typo-tolerant) keyword matching.',
		},
	];

	const LIMITS = [
		'Image search = visual frame similarity, not face / identity recognition.',
		'No speaker diarization — nothing links who is on screen to who is speaking.',
		'The reranker is text-only; it never uses the image.',
		'Multi-leg fusion (up to 4 judges) is equal-weight — there is no per-leg weight yet.',
		'Scene depends on AI captions — only as accurate as the frame captioner.',
	];

	// Decision guide: situation → which search (fact-checked; "all" = up to 4 legs).
	// The answer column is `use`, not `then`: a plain object with a `then` property is a THENABLE,
	// and awaiting anything holding one silently calls it (unicorn/no-thenable).
	const DECISION = [
		{
			when: 'You can quote the exact words or a name that was said',
			use: 'Keyword — type the phrase (Phrase for exact order, Fuzzy for typos). Turn on Rerank to float the closest wording up.',
		},
		{
			when: 'You only remember the gist, not the words',
			use: 'Vector — describe the idea in your own words; it matches meaning, so paraphrases and synonyms still land.',
		},
		{
			when: 'You remember what it LOOKED like, not what was said',
			use: "Scene — describe the picture (podium, placard, snowy street). It searches the AI's caption of each frame, not the pixels or the speech.",
		},
		{
			when: 'You have a screenshot / photo from the clip',
			use: 'Image — attach it to find visual look-alikes. Look-alike only, not face / identity; add words if you also recall what was said.',
		},
		{
			when: 'You want exact words AND meaning',
			use: 'Hybrid — runs Keyword + Vector together; nudge the Balance slider toward keyword (precise) or meaning (looser).',
		},
		{
			when: "You're not sure which clue is strongest",
			use: 'Attach an image and add text → all-angles mode: up to four legs (Keyword, Vector, Scene, Image) vote equally; the dial is ignored.',
		},
	];

	// Results troubleshooter: symptom → fix (fact-checked; postfilter can drop <N).
	const TROUBLESHOOT = [
		{
			symptom: 'A top hit has nothing to do with what is on screen',
			fix: 'It won on the transcript — someone SAID your words even if the frame shows something else. Use Scene if you meant what was SHOWN.',
		},
		{
			symptom: 'You attached an image but look-alikes are not dominating',
			fix: 'Expected: in mixed search the image is one equal vote (RRF), never a gate. Drop the text for Image-only, or add Scene to reinforce the look.',
		},
		{
			symptom: 'Fewer results came back than the count you set',
			fix: 'A Filter is dropping them — a postfilter trims after searching. Loosen/remove it, switch it to prefilter, or use Vector to widen recall.',
		},
		{
			symptom: 'Off-topic hits on top, good ones lower down',
			fix: 'Turn on Rerank to re-sort the top by transcript, push the Balance slider toward the signal you trust, or add a Filter.',
		},
		{
			symptom: 'Results feel noisy, scattered across press conferences',
			fix: 'Narrow with Filters (name, language, referenskod). With prefilter on they narrow the transcript legs (Keyword/Vector) before searching; the Image/Scene legs always filter after ranking.',
		},
		{
			symptom: 'Sure a chunk exists but nothing finds it',
			fix: 'Widen recall: add signals (Hybrid or Scene), raise the result count, clear Filters. Wrong exact words? Vector catches the meaning Keyword misses.',
		},
		{
			symptom: 'Rerank is on but the order barely changed',
			fix: 'Rerank reads transcripts only — a no-op for Image, and it ignores captions. For visual / scene intent, fix the query or signal instead.',
		},
	];

	// Plain-language glossary (fact-checked rerank-scope wording).
	const GLOSSARY = [
		{
			term: 'Chunk',
			def: 'one flashcard — a few seconds of clip, with the spoken transcript on one side and the video frame (+ caption) on the other. The smallest thing search returns.',
		},
		{
			term: 'Caption',
			def: 'a short Swedish sentence an AI wrote about what is VISIBLE in the frame. Lets you find things shown but never spoken.',
		},
		{
			term: 'Keyword (BM25)',
			def: 'the literal-minded librarian — finds the exact words you typed (with phrases + small typo-tolerance) in the transcript. Great for names/quotes, blind to synonyms.',
		},
		{
			term: 'Embedding / Vector',
			def: "turns text into a dot on a giant 'meaning map' so similar ideas land near each other. Vector search returns transcripts whose dot sits closest to your query, even with no shared words.",
		},
		{
			term: 'Cosine',
			def: 'the ruler for "how close" two dots point on the meaning map. Same direction = alike. Just the yardstick, not a setting.',
		},
		{
			term: 'RRF',
			def: 'the fair vote-counter — rewards results that rank HIGH on MORE lists, using ranks not raw scores. (The Balance slider is the exception: it blends raw scores, Hybrid only.)',
		},
		{
			term: 'Cross-encoder / Rerank',
			def: "the careful second reader — re-reads each candidate's transcript against your text and reshuffles the head. It scores by transcript only, so it can't use the image or caption signal — but it still re-ranks whatever candidates sit in the head.",
		},
		{
			term: 'Prefilter vs Postfilter',
			def: 'Filters are a guest list. Prefilter checks IDs at the door (search only looks inside allowed chunks); Postfilter lets everyone search then removes the uninvited after — so you may get fewer results than you asked for.',
		},
		{
			term: 'Scene',
			def: 'searching the PICTURE side with text — compares your words to the frame captions, surfacing what is on screen even when unspoken.',
		},
		{
			term: 'Image',
			def: 'drag in a picture to find visual look-alikes (similar frames). Matches appearance, not who a person is.',
		},
	];

	// Data-flow: the journey of a query end-to-end (from services/search/services/service.py).
	const JOURNEY = [
		{
			label: 'You ask',
			detail: 'your text and/or attached image (plus any Filters) go to the backend.',
		},
		{
			label: 'Words & pictures → vectors',
			detail:
				'the embedding service (a vLLM model) turns your text into a meaning-vector and your image into an image-vector. Skipped for pure Keyword and Scene-keyword — those are word-matching, no vectors.',
		},
		{
			label: 'Each signal queries a table',
			detail:
				'Keyword & Vector read the chunks table (transcripts); Image & Scene read the chunk_frames table (one row per video frame), then join each frame back to its chunk.',
		},
		{
			label: 'Merge',
			detail:
				"the backend fuses the per-signal ranked lists — RRF, or Lance's native hybrid / the Balance slider.",
		},
		{
			label: 'Optional rerank',
			detail:
				'the reranker service (a second vLLM model) re-reads the top transcripts and reorders just the head.',
		},
		{
			label: 'Dress the results',
			detail: "the backend attaches each hit's caption and word-level alignments.",
		},
		{
			label: 'Back to you',
			detail:
				'one uniform result shape for every mode; click a hit and the player plays it with the transcript synced.',
		},
	];

	// Scenario matrix: the dial × image → mode/passes mapping, straight from
	// buildSpec (search-bar.svelte) + run_search (service.py).
	const SCENARIOS = [
		{
			give: 'Keyword — text, no image',
			mode: 'fts',
			passes: 'Keyword (BM25)',
			combined: 'single list',
			slider: '—',
			rerank: 'yes',
		},
		{
			give: 'Vector — text, no image',
			mode: 'semantic',
			passes: 'Vector (text embedding)',
			combined: 'single list',
			slider: '—',
			rerank: 'yes',
		},
		{
			give: 'Scene — text, no image',
			mode: 'scene',
			passes: 'Scene caption (meaning, or BM25 via the Scene toggle)',
			combined: 'single list',
			slider: '—',
			rerank: 'yes',
		},
		{
			give: 'Hybrid — text, no image',
			mode: 'hybrid',
			passes: 'Keyword + Vector',
			combined: 'RRF, or the Balance slider',
			slider: 'available',
			rerank: 'yes',
		},
		{
			give: 'Image only — no text',
			mode: 'visual',
			passes: 'Image (frame)',
			combined: 'single list',
			slider: '—',
			rerank: 'no-op (no text)',
		},
		{
			give: 'Keyword + image + text',
			mode: 'all',
			passes: 'Keyword + Vector + Image + Scene',
			combined: 'equal-weight RRF',
			slider: 'ignored',
			rerank: 'yes',
			highlight: true,
		},
		{
			give: 'Vector + image + text',
			mode: 'all',
			passes: 'Keyword + Vector + Image + Scene',
			combined: 'equal-weight RRF',
			slider: 'ignored',
			rerank: 'yes',
			highlight: true,
		},
		{
			give: 'Scene + image + text',
			mode: 'all',
			passes: 'Keyword + Vector + Image + Scene',
			combined: 'equal-weight RRF',
			slider: 'ignored',
			rerank: 'yes',
			highlight: true,
		},
		{
			give: 'Hybrid + image + text',
			mode: 'all',
			passes: 'Keyword + Vector + Image + Scene',
			combined: 'equal-weight RRF',
			slider: 'ignored',
			rerank: 'yes',
			highlight: true,
		},
	];

	// Interactive scenario explorer — reactively mirrors buildSpec so toggling the
	// dial / text / image animates which passes run and how they fuse.
	let dial = $state<'keyword' | 'vector' | 'scene' | 'hybrid'>('hybrid');
	let hasText = $state(true);
	let hasImage = $state(false);

	const demoMode = $derived.by(() => {
		if (hasImage && hasText) return 'all';
		if (hasImage) return 'visual';
		if (!hasText) return null;
		return dial === 'keyword'
			? 'fts'
			: dial === 'vector'
				? 'semantic'
				: dial === 'scene'
					? 'scene'
					: 'hybrid';
	});
	const demoLegs = $derived.by((): string[] => {
		switch (demoMode) {
			case 'all':
				return ['keyword', 'vector', 'image', 'scene'];
			case 'hybrid':
				return ['keyword', 'vector'];
			case 'fts':
				return ['keyword'];
			case 'semantic':
				return ['vector'];
			case 'scene':
				return ['scene'];
			case 'visual':
				return ['image'];
			default:
				return [];
		}
	});
	const demoSlider = $derived(demoMode === 'hybrid');
	const demoRerank = $derived(demoMode !== 'visual' && demoMode !== null);

	const DEMO_DIALS = ['keyword', 'vector', 'scene', 'hybrid'] as const;
	const DEMO_LANES = [
		{
			key: 'keyword',
			name: 'Keyword',
			icon: '⌨️',
			side: 'said',
			on: 'border-sky-400 bg-sky-400/10 text-sky-600 dark:text-sky-300',
		},
		{
			key: 'vector',
			name: 'Vector',
			icon: '💬',
			side: 'said',
			on: 'border-primary bg-primary/10 text-primary',
		},
		{
			key: 'image',
			name: 'Image',
			icon: '🖼️',
			side: 'shown',
			on: 'border-amber-400 bg-amber-400/10 text-amber-600 dark:text-amber-300',
		},
		{
			key: 'scene',
			name: 'Scene',
			icon: '🎬',
			side: 'shown',
			on: 'border-emerald-400 bg-emerald-400/10 text-emerald-600 dark:text-emerald-300',
		},
	];

	// Interactive worked examples (generated + fact-checked; RRF fractions and the
	// postfilter over-fetch numbers corrected per the critic).
	let exIdx = $state(0);
	const WORKED = [
		{
			key: 'hybrid',
			tab: 'Paraphrase → Hybrid',
			title:
				'A minister talked about helping households with their electricity bills — but I forget the exact words.',
			mode: 'hybrid',
			why: 'You half-remember the topic but not the phrasing, so you need exact-word AND meaning matching at once — that is Hybrid (Keyword + Vector).',
			steps: [
				{
					stage: 'Filters (none set)',
					yields: 'No filter → both legs search the whole corpus, nothing narrowed away.',
				},
				{
					stage: 'Keyword leg — BM25 on transcript',
					yields:
						"Matches only literal spoken words of your query 'hjälpa hushåll med elräkningar'. #1 A 'hjälpa hushåll som inte klarar sina elräkningar'; #2 E 'hushållens elräkningar har tredubblats'. The paraphrase C is ABSENT — it never says your words, so BM25 scores it 0.",
				},
				{
					stage: 'Vector leg — cosine on text_embedding',
					yields:
						"Matches meaning, ignores shared words. #1 C 'vi vill stötta hushållen med elkostnaderna'; #2 A. C wins here despite zero word overlap with your query.",
				},
				{
					stage: 'RRF fuse — rank-based, equal weight',
					yields:
						'A is on BOTH lists (Keyword #1, Vector #2) → highest combined score. C is on ONE list but at #1 → just behind. Fused order: A, C, then the rest. A leads only because it scored on both legs.',
				},
				{
					stage: 'Rerank top-K — cross-encoder, transcript-only',
					yields:
						"Re-reads each candidate's transcript against your text. Recognises 'stötta hushållen med elkostnaderna' as the closest paraphrase and floats C to #1, A #2. (Ignores frames/captions.)",
				},
			],
			result:
				"C — 'vi vill stötta hushållen med elkostnaderna' — wins, despite sharing no word with your query. Vector found it by meaning, RRF kept it near the top, and the text-only rerank promoted it to outright #1.",
			takeaway:
				'Remember the idea but not the words? Hybrid covers both — Keyword nails any literal word that was said, Vector rescues the differently-phrased clip, and rerank breaks the tie by re-reading transcripts.',
		},
		{
			key: 'scene',
			tab: 'Snowy street → Scene',
			title: 'It was filmed on a snowy street — but I forget what they said.',
			mode: 'scene',
			why: "You remember the picture, not the words — so the only signal that can win is the one that reads each frame's caption of what is VISIBLE. That's Scene.",
			steps: [
				{
					stage: 'Pick the mode',
					yields:
						"Type the picture in words: 'snöig gata utomhus', dial = Scene (no image attached, so not 'all'). Backend mode = scene.",
				},
				{
					stage: 'Why NOT Keyword',
					yields:
						"Keyword runs BM25 on the TRANSCRIPT only. This clip's transcript is energy talk ('elpriserna har stigit...') — 'snö' / 'gata' are never spoken, so BM25 scores it 0. It would instead surface a clip that literally says 'gata' (a street-repair debate) — the wrong scene.",
				},
				{
					stage: 'Why NOT Vector',
					yields:
						"Vector embeds the TRANSCRIPT and compares to your query. The transcript is about elpriser — far from 'snöig gata' in meaning space — so the clip ranks ~#80, effectively missed. Vector reads what was SAID, never what is shown.",
				},
				{
					stage: 'Scene leg — the one pass',
					yields:
						"Your text → one vector, compared by cosine to every frame's caption_embedding, then joined back to its chunk. Top captions: #1 .81 'minister intervjuas på en snöig gata utomhus'; #2 .77 'reporter på en snötäckt trottoar'; #3 .69 'vinterväg med bilar, snö faller'.",
				},
				{
					stage: 'Fuse — nothing to fuse',
					yields:
						'Scene is a single leg: no RRF, no Balance slider. The cosine-ranked caption list IS the result.',
				},
				{
					stage: 'Rerank — would it help? No',
					yields:
						"Rerank re-reads only each candidate's TRANSCRIPT vs your text. 'snöig gata' has no match in the energy transcript, so it would score the right clip LOW and could push it down — and it never reads the caption that won. Leave it off for visual intent.",
				},
			],
			result:
				"The chunk behind caption #1 — 'minister intervjuas på en snöig gata utomhus' (.81). Scene matched your text against the frame's caption of what's on screen, not the spoken energy transcript that Keyword and Vector were stuck reading.",
			takeaway:
				"Remember the LOOK, not the words? Use Scene — it reads each frame's caption, so a clip whose transcript is pure energy policy still surfaces from its snowy-street visual. Don't lean on Rerank, which only re-reads transcripts.",
		},
		{
			key: 'all',
			tab: 'Photo + word → all',
			title:
				"Here's a screenshot of a podium with mics — and I think it was the press conference about the export stop.",
			mode: 'all',
			why: "Two independent clues — a photo AND a remembered word ('exportstopp'). Attach the image and type the text → the 4-leg 'all' mode, where each clue votes equally.",
			steps: [
				{
					stage: 'Build the spec — dial IGNORED',
					yields:
						"You drag in podium.png and type 'exportstopp'. Image + text → mode = all no matter where the dial sits; the Balance slider is dropped. Your text drives the Keyword + Vector + Scene legs; the image drives the Image leg. Each leg over-fetches ~3×N candidates.",
				},
				{
					stage: 'Leg 1 — Keyword (BM25 on transcript)',
					yields:
						"Literal 'exportstopp': #1 chunk_842 'vi inför ett exportstopp för...'; #2 chunk_119 'exportstoppet träder i kraft'. Even though you 'meant' Vector by habit, your text still ran a real BM25 keyword leg.",
				},
				{
					stage: 'Leg 2 — Vector (meaning on text_embedding)',
					yields:
						"Paraphrases with no shared word: #1 chunk_507 'stoppa utförseln av krigsmateriel'; #2 chunk_842. chunk_842 ranks well here too.",
				},
				{
					stage: 'Leg 3 — Image (frame_embedding vs your photo)',
					yields:
						'Pure look-alike, no text: #1 chunk_077 (near-identical mic cluster); #2 chunk_842 (same podium room); #3 chunk_415. It found visually-similar podiums; it does NOT know who is standing there.',
				},
				{
					stage: 'Leg 4 — Scene (caption_embedding vs your text)',
					yields:
						"Your text vs each frame's caption: #1 chunk_842 'minister vid talarstol, plakat om vapenexport'; #2 chunk_203 'diagram över exportsiffror'. Your text drove a second picture-side leg.",
				},
				{
					stage: 'RRF fuse — equal weight, rank-based',
					yields:
						'chunk_842 appears near the top of ALL FOUR lists → highest combined score. chunk_077 is in 2 lists, chunk_507 in 2. Nobody is the unanimous #1, but chunk_842 is high on the MOST lists → it wins. The image was one vote of four, never a gate.',
				},
				{
					stage: 'Rerank the head — text-only, top-K',
					yields:
						"Reads ONLY transcripts vs 'exportstopp'. chunk_842 literally says 'vi inför ett exportstopp' → stays #1. A look-alike like chunk_415 (right podium, transcript about elpriser) gets pushed DOWN — rerank can't see its frame, only its off-topic words.",
				},
			],
			result:
				'chunk_842 — the minister at the podium actually announcing the export stop. It was the only chunk near the top of all four legs at once; RRF rewards that agreement, and the text-only rerank confirmed it via its on-topic transcript.',
			takeaway:
				"Two kinds of memory — a picture AND a word? Attach both: 'all' runs four equal legs (the dial is irrelevant), so your text still fires a BM25 leg and a Scene leg alongside the image. The clip high on the most legs wins; the image is a vote, never a filter.",
		},
		{
			key: 'postfilter',
			tab: 'Too few → prefilter',
			title:
				"I filtered by a minister's name and got far fewer hits than my count — and the good ones vanished.",
			mode: 'hybrid',
			why: "You want every clip where Andersson discusses elstöd, so Hybrid gives the best recall — but the Filter is set to postfilter, which silently drops most of each leg's results.",
			steps: [
				{
					stage: 'Setup',
					yields:
						"Mode = Hybrid. Query 'elstöd till hushållen'. Count N = 20. Filter namn LIKE '%Andersson%'. Filter toggle = POSTFILTER. Hybrid runs Keyword + Vector over the whole corpus, then fuses by RRF.",
				},
				{
					stage: 'Postfilter = rank everyone, then drop',
					yields:
						'Each leg ranks the WHOLE corpus (mostly other ministers) and keeps its candidate pool, THEN deletes the non-Andersson rows. Andersson is only a fraction of each pool, so few survive — the fused list comes back well short of 20.',
				},
				{
					stage: 'The vanished clip',
					yields:
						"A clip you remember ('elstödet kommer redan i januari', Andersson) ranks low corpus-wide — it competes against every minister — so it falls outside each leg's fetched pool and is dropped before you ever see it. That's your missing good one.",
				},
				{
					stage: 'Diagnosis',
					yields:
						'Postfilter applies the Filter AFTER each leg ranks the full corpus, so your count (20) is the size BEFORE the drop, not what is delivered — and anything ranked low across the whole corpus is invisible.',
				},
				{
					stage: 'Fix — flip to PREFILTER',
					yields:
						"Set the Filter to prefilter (the default; the backend passes prefilter=True). Now the Filter narrows the corpus to ONLY Andersson's chunks BEFORE either leg ranks.",
				},
				{
					stage: 'Result after prefilter',
					yields:
						"Each leg now ranks within Andersson only, so it returns a full 20 — and 'elstödet kommer redan i januari' competes only against other Andersson clips, where it ranks near the top of both legs, so RRF puts it high.",
				},
			],
			result:
				"Flipping postfilter → prefilter turns ~5 results into a full 20, and the previously-missing clip lands near the top — because prefilter lets it compete only against Andersson's chunks instead of the whole corpus.",
			takeaway:
				'A metadata Filter on postfilter ranks the whole corpus then DROPS non-matches → fewer than your count, and any match ranked low corpus-wide disappears. Switch to prefilter to narrow first and let every leg return a full N from that subset.',
		},
	];
</script>

<div class="h-full overflow-y-auto">
	<div class="mx-auto max-w-3xl px-6 py-8">
		<!-- header + tabs -->
		<div class="mb-6">
			<div
				class="text-primary mb-1 flex items-center gap-2 text-xs font-medium tracking-wide uppercase"
			>
				<Sparkles class="size-3.5" /> Guide
			</div>
			<h1 class="text-foreground text-2xl font-semibold tracking-tight">How the viewer works</h1>
			<div class="border-border mt-4 flex gap-1 border-b">
				<button
					type="button"
					onclick={() => (tab = 'search')}
					class={'flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-medium transition-colors ' +
	(tab === 'search'
		? 'border-primary text-foreground'
		: 'text-muted-foreground hover:text-foreground border-transparent')}
				>
					<Search class="size-4" /> Search
				</button>
				<button
					type="button"
					onclick={() => (tab = 'atlas')}
					class={'flex items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-medium transition-colors ' +
	(tab === 'atlas'
		? 'border-primary text-foreground'
		: 'text-muted-foreground hover:text-foreground border-transparent')}
				>
					<MapIcon class="size-4" /> Atlas
				</button>
			</div>
		</div>

		{#if tab === 'search'}
			<!-- ░░ SEARCH ░░ -->
			<!-- 0 · frame -->
			<div use:reveal class="reveal bg-card border-border mb-6 rounded-lg border px-4 py-3 text-sm">
				<span class="text-foreground">Every result is one </span>
				<strong class="text-primary">chunk</strong>
				<span class="text-muted-foreground">
					= a short transcript span + the video frame from that moment. Search ranks chunks.
				</span>
			</div>

			<!-- A · core mental model: said vs shown -->
			<section use:reveal class="reveal mb-10">
				<h2 class="text-foreground mb-1 text-lg font-semibold">
					Two worlds: was it said, or was it shown?
				</h2>
				<p class="text-muted-foreground mb-4 text-sm leading-relaxed">
					The one question to ask before searching. Every chunk has two sides — the words that were
					<strong class="text-foreground">said</strong> (the transcript) and the moment that was
					<strong class="text-foreground">shown</strong> (the frame + its Swedish caption). Each signal reads
					one side, so figure out which side your memory lives on and the right tool almost picks itself.
				</p>
				<div class="grid gap-3 sm:grid-cols-2">
					<div use:reveal class="reveal rounded-lg border border-sky-400/40 bg-sky-400/5 p-3">
						<div class="text-foreground font-medium">🗣️ Said — the transcript</div>
						<p class="text-muted-foreground mt-1 text-xs leading-relaxed">
							What someone spoke. Read by <span class="font-medium text-sky-500 dark:text-sky-300"
								>Keyword</span
							>
							(exact words) and <span class="text-primary font-medium">Vector</span> (the meaning, even in other
							words). Neither looks at the picture.
						</p>
					</div>
					<div
						use:reveal={{ delay: 90 }}
						class="reveal rounded-lg border border-emerald-400/40 bg-emerald-400/5 p-3"
					>
						<div class="text-foreground font-medium">👁️ Shown — the frame</div>
						<p class="text-muted-foreground mt-1 text-xs leading-relaxed">
							What was on screen. Read by
							<span class="font-medium text-emerald-500 dark:text-emerald-300">Scene</span>
							(describe it in words → matches the caption) and
							<span class="font-medium text-amber-500 dark:text-amber-300">Image</span>
							(hand it a photo → visual look-alikes).
						</p>
					</div>
				</div>
				<p class="text-muted-foreground mt-3 text-sm leading-relaxed">
					Not sure which world? Attach an image and add words — that opens the all-angles mode, which
					votes across both worlds at once (the image is one vote, never the boss). And note: <strong
						class="text-foreground">Rerank</strong
					>
					only re-reads the <em>said</em> side, and <strong class="text-foreground">Filters</strong>
					aren't a world — they narrow the room <em>before</em> (or, as a postfilter, after) every search.
				</p>
			</section>

			<!-- B · what an embedding is -->
			<section use:reveal class="reveal mb-10">
				<h2 class="text-foreground mb-1 text-lg font-semibold">What "meaning search" actually is</h2>
				<p class="text-muted-foreground mb-3 text-sm leading-relaxed">
					Picture a vast map where every idea gets a pin, and similar meanings land near each other —
					"the minister stepped down" sits a short walk from "the minister resigned", even with no shared
					word. An
					<strong class="text-foreground">embedding</strong> is just that pin. Vector search drops your query
					on the same map and hands back the nearest chunks — so wording barely matters.
				</p>
				<div class="border-border bg-muted rounded-lg border p-3 text-xs leading-relaxed">
					<ul class="text-muted-foreground space-y-1.5">
						<li>
							<strong class="text-foreground">Embedding = a map pin for meaning.</strong> Close = similar; far
							= unrelated. That distance is the search.
						</li>
						<li>
							<strong class="text-foreground">"Cosine" = pointing the same direction.</strong> Two ideas headed
							the same way are a match — length / loudness doesn't matter.
						</li>
						<li>
							<strong class="text-foreground">One shared map for text AND captions.</strong> Transcript-meanings
							and caption-meanings live in the same space, so typed words can find a frame's caption — that's
							the trick behind Scene.
						</li>
						<li>
							<strong class="text-foreground">Meaning, not letters.</strong> Vector shrugs off spelling but can
							drift on names / codes / quotes — that's where Keyword (or Filters) earns its keep.
						</li>
					</ul>
				</div>
			</section>

			<!-- C · why several signals + fuse -->
			<section use:reveal class="reveal mb-10">
				<h2 class="text-foreground mb-1 text-lg font-semibold">
					Why cast several nets, then pull them together
				</h2>
				<p class="text-muted-foreground mb-3 text-sm leading-relaxed">
					No single signal sees the whole moment — Keyword and Vector only hear the words; Scene and
					Image only see the picture. So the app asks several witnesses at once — a stenographer, a
					paraphraser, a set photographer — and trusts the clips they <em>agree</em> on. Agreement across independent
					signals is the strongest hint a clip is really relevant.
				</p>
				<div class="border-border bg-muted rounded-lg border p-3 text-xs leading-relaxed">
					<ul class="text-muted-foreground space-y-1.5">
						<li>
							<strong class="text-foreground">Wide net first, tighten later.</strong> In the fused
							<code class="font-mono">all</code> mode each leg over-fetches ~3× more candidates than you asked
							for (recall — don't miss it), then fusion + Rerank squeeze to the best few (precision).
						</li>
						<li>
							<strong class="text-foreground">Agreement is the relevance signal.</strong> Appearing once is a
							maybe; ranking high on three lists is a strong yes.
						</li>
						<li>
							<strong class="text-foreground">Votes, not vetoes.</strong> In image-plus-text search every leg
							is one equal vote (RRF); a missing leg just abstains and the rest decide.
						</li>
						<li>
							<strong class="text-foreground">Catches synonyms AND silence.</strong> Vector saves you when the
							words differ; Scene saves you when the thing on screen was never spoken.
						</li>
					</ul>
				</div>
			</section>

			<!-- 2 · MULTI-PASS vs PREFILTER — the centerpiece -->
			<section use:reveal class="reveal mb-10">
				<h2 class="text-foreground mb-1 text-lg font-semibold">
					Is "image + keyword/vector" multiple passes, or a prefilter?
				</h2>
				<p class="text-muted-foreground mb-4 text-sm leading-relaxed">
					<strong class="text-foreground"
						>Multiple independent passes, merged by rank — never a prefilter.</strong
					>
					The moment you attach an image <em>and</em> have text, the Keyword/Vector/Hybrid dial stops mattering:
					the tool runs up to four searches side by side, each scanning the whole library on its own, then
					merges their rankings.
				</p>

				<!-- parallel-lanes diagram -->
				<div class="bg-card border-border rounded-xl border p-4">
					<div class="text-muted-foreground mb-2 text-center text-xs">
						every pass starts from the <strong class="text-foreground">same full library</strong> — all at once
					</div>
					<div class="grid grid-cols-2 gap-2 sm:grid-cols-4">
						{#each LANES as lane, i (lane.name)}
							<div
								use:reveal={{ delay: i * 80 }}
								class="reveal bg-muted border-border relative overflow-hidden rounded-lg border p-2.5 text-center"
							>
								<div class="{lane.text} text-xs font-semibold">{lane.name}</div>
								<div class="text-muted-foreground text-[0.7rem]">{lane.sub}</div>
								<div class="text-muted-foreground mt-1 text-[0.7rem]">→ own ranked list</div>
								<span class="lane-scan {lane.scan}"></span>
							</div>
						{/each}
					</div>
					<div class="text-muted-foreground my-1 text-center text-sm">▼&ensp;▼&ensp;▼&ensp;▼</div>
					<div
						class="border-primary/50 bg-primary/10 text-foreground rounded-lg border px-3 py-2 text-center text-sm font-medium"
					>
						Merge by rank — RRF (equal weight). High in <em>more</em> lists → wins.
					</div>
					<div class="text-muted-foreground my-1 text-center text-sm">▼</div>
					<div
						class="bg-background border-border text-foreground rounded-lg border px-3 py-2 text-center text-sm font-semibold"
					>
						Top N results
					</div>
				</div>

				<!-- DO / DON'T -->
				<div class="mt-3 grid gap-3 sm:grid-cols-2">
					<div
						use:reveal
						class="reveal rounded-lg border border-emerald-500/40 bg-emerald-500/5 p-3 text-xs leading-relaxed"
					>
						<div class="mb-1 font-semibold text-emerald-600 dark:text-emerald-300">
							✓ What actually happens
						</div>
						<p class="text-muted-foreground">
							Four searchlights sweep the same full library at once; their rankings are pooled. A chunk the
							image never matched can still rank #1 on the strength of its transcript alone — the image is
							a
							<strong class="text-foreground">voice in the vote</strong>, not a gate.
						</p>
					</div>
					<div
						use:reveal={{ delay: 90 }}
						class="reveal border-destructive/40 bg-destructive/5 rounded-lg border p-3 text-xs leading-relaxed"
					>
						<div class="text-destructive mb-1 font-semibold line-through">✗ Not this</div>
						<p class="text-muted-foreground">
							The image runs first, grabs a shortlist of look-alike clips, and the text then searches
							<em>only inside</em> that shortlist.
							<strong class="text-foreground">No such funnel exists.</strong>
							(Only the <em>Filters</em> panel narrows first — see below.)
						</p>
					</div>
				</div>

				<!-- traced example -->
				<div
					use:reveal
					class="reveal bg-muted border-border mt-3 rounded-lg border p-3 text-xs leading-relaxed"
				>
					<div class="text-foreground mb-1 font-medium">
						Traced: <code class="font-mono">"arbete"</code> + a photo of a snowy street
					</div>
					<p class="text-muted-foreground">
						Four passes fire at once. <strong class="text-foreground">Keyword</strong> ranks a
						labour-market briefing #1; <strong class="text-foreground">Vector</strong> surfaces a clip
						about "sysselsättning" (no exact word) at #2;
						<strong class="text-foreground">Image</strong>
						ranks an outdoor winter stakeout by frame-similarity;
						<strong class="text-foreground">Scene</strong> lifts a frame captioned "person på snöig gata". A
						clip that lands #3 / #5 / #3 / #2 across all four lists beats the briefing that was #1 in only one
						— agreement wins. The photo never walled off a subset that "arbete" then searched within.
					</p>
				</div>
			</section>

			<!-- 2b · order of operations / priority -->
			<section use:reveal class="reveal mb-10">
				<h2 class="text-foreground mb-1 text-lg font-semibold">
					In what order does it happen? (and what's prioritised)
				</h2>
				<p class="text-muted-foreground mb-4 text-sm leading-relaxed">
					The search legs have <strong class="text-foreground">no priority over each other</strong> — they're
					equal-weight and run at the same time. So "image + Hybrid (FTS + Vector)" doesn't rank one before
					another; the only real ordering is these pipeline stages:
				</p>
				<ol class="space-y-3">
					<li use:reveal class="reveal flex gap-3">
						<span
							class="bg-primary text-primary-foreground grid size-6 shrink-0 place-items-center rounded-full text-xs font-semibold"
							>1</span
						>
						<div>
							<div class="text-foreground text-sm font-medium">
								Filters first <span class="text-muted-foreground font-normal">(optional)</span>
							</div>
							<p class="text-muted-foreground mt-0.5 text-xs leading-relaxed">
								Your metadata Filters (language, name, referenskod, extraid, raw SQL) become a
								<code class="font-mono">WHERE</code>. For the
								<strong class="text-foreground">transcript legs</strong>
								(Keyword, Vector, Hybrid), <em>prefilter</em> narrows the corpus <em>before</em>
								they search (toggle off → after); the
								<strong class="text-foreground">frame legs</strong>
								(Image, Scene) always apply it
								<em>after</em> ranking. Either way it's the only narrowing step. No filters set → the whole library.
							</p>
						</div>
					</li>
					<li use:reveal={{ delay: 70 }} class="reveal flex gap-3">
						<span
							class="bg-primary text-primary-foreground grid size-6 shrink-0 place-items-center rounded-full text-xs font-semibold"
							>2</span
						>
						<div>
							<div class="text-foreground text-sm font-medium">Every leg runs in parallel</div>
							<p class="text-muted-foreground mt-0.5 text-xs leading-relaxed">
								Keyword, Vector, Image and Scene each search independently and <strong
									class="text-foreground">at the same time</strong
								>. No leg goes first and none outranks another. In
								<code class="font-mono">all</code>
								mode each over-fetches ~3×N candidates so good chunks aren't lost before the merge (single-leg
								searches fetch exactly your count).
							</p>
						</div>
					</li>
					<li use:reveal={{ delay: 140 }} class="reveal flex gap-3">
						<span
							class="bg-primary text-primary-foreground grid size-6 shrink-0 place-items-center rounded-full text-xs font-semibold"
							>3</span
						>
						<div>
							<div class="text-foreground text-sm font-medium">Fuse by rank — equal weight (RRF)</div>
							<p class="text-muted-foreground mt-0.5 text-xs leading-relaxed">
								Each leg's ranked list contributes <code class="font-mono">1/(60 + rank)</code> per chunk; a
								chunk high in <em>more</em> lists wins. Every leg counts the same. (Hybrid
								<em>without</em> an image may instead use the Balance slider — a 2-way blend of raw scores; adding
								an image always reverts to equal-weight RRF.)
							</p>
						</div>
					</li>
					<li use:reveal={{ delay: 210 }} class="reveal flex gap-3">
						<span
							class="bg-primary text-primary-foreground grid size-6 shrink-0 place-items-center rounded-full text-xs font-semibold"
							>4</span
						>
						<div>
							<div class="text-foreground text-sm font-medium">
								Rerank the head <span class="text-muted-foreground font-normal">(optional)</span>
							</div>
							<p class="text-muted-foreground mt-0.5 text-xs leading-relaxed">
								A text-only cross-encoder re-scores just the top <em>K</em> of the fused list, reading each
								chunk's transcript (the <code class="font-mono">text</code> column) against your text — see below.
							</p>
						</div>
					</li>
					<li use:reveal={{ delay: 280 }} class="reveal flex gap-3">
						<span
							class="bg-primary text-primary-foreground grid size-6 shrink-0 place-items-center rounded-full text-xs font-semibold"
							>5</span
						>
						<div>
							<div class="text-foreground text-sm font-medium">Trim to top N</div>
							<p class="text-muted-foreground mt-0.5 text-xs leading-relaxed">
								The fused (and optionally reranked) list is cut to N — the results you see.
							</p>
						</div>
					</li>
				</ol>
			</section>

			<!-- 3 · the 4 judges -->
			<section use:reveal class="reveal mb-10">
				<h2 class="text-foreground mb-1 text-lg font-semibold">The 4 judges</h2>
				<p class="text-muted-foreground mb-4 text-sm leading-relaxed">
					A search asks up to four independent judges to rank the chunks. Each runs as its
					<em>own pass</em> over its <em>own</em> space and hands back its <em>own</em> ranked list — they
					never compare scores with each other.
				</p>
				<div class="grid gap-3 sm:grid-cols-2">
					{#each JUDGES as j, i (j.mode)}
						<div
							use:reveal={{ delay: i * 90 }}
							class="reveal bg-muted border-border {j.accent} rounded-lg border border-l-[3px] p-3"
						>
							<div class="flex items-center gap-2">
								<span class="text-base leading-none">{j.icon}</span>
								<span class="text-foreground font-medium">{j.name}</span>
								<code class="bg-card text-primary ml-auto rounded-md px-1.5 py-0.5 font-mono text-xs">
									mode: {j.mode}
								</code>
							</div>
							<p class="text-muted-foreground mt-1.5 text-xs leading-relaxed">{j.what}</p>
						</div>
					{/each}
				</div>
			</section>

			<!-- 4 · what each judge does (table) -->
			<section use:reveal class="reveal mb-10">
				<h2 class="text-foreground mb-3 text-lg font-semibold">What each judge actually does</h2>
				<div class="border-border overflow-hidden rounded-lg border text-xs">
					<div class="bg-muted text-foreground grid grid-cols-[84px_1fr_1.4fr] font-medium">
						<div class="border-border border-b p-2">Judge</div>
						<div class="border-border border-b border-l p-2">You give it</div>
						<div class="border-border border-b border-l p-2">Compared against</div>
					</div>
					{#each JUDGES as j, i (j.mode)}
						<div
							use:reveal={{ delay: i * 70 }}
							class="reveal text-muted-foreground grid grid-cols-[84px_1fr_1.4fr] last:[&>div]:border-b-0"
						>
							<div class="border-border text-foreground border-b p-2">{j.icon} {j.name}</div>
							<div class="border-border border-b border-l p-2">{j.give}</div>
							<div class="border-border border-b border-l p-2">{j.vs}</div>
						</div>
					{/each}
				</div>
				<div
					use:reveal
					class="reveal border-primary/40 bg-primary/5 text-muted-foreground mt-3 rounded-lg border border-dashed p-3 text-sm"
				>
					💡 Your <strong class="text-foreground">text</strong> drives
					<strong class="text-foreground">two</strong>
					passes at once: the <span class="text-primary font-medium">Meaning</span> leg (what was
					<em>said</em>) and the
					<span class="font-medium text-emerald-500 dark:text-emerald-300">Scene</span>
					leg (what's
					<em>shown</em>, from the frame caption).
				</div>
			</section>

			<!-- 4b · signals in action (concrete examples) -->
			<section use:reveal class="reveal mb-10">
				<h2 class="text-foreground mb-1 text-lg font-semibold">See each signal in action</h2>
				<p class="text-muted-foreground mb-4 text-sm leading-relaxed">
					One concrete query for each — what it finds, what it misses, and when to reach for it.
				</p>
				<div class="grid gap-3 sm:grid-cols-2">
					{#each SIGNAL_EXAMPLES as s, i (s.name)}
						<div
							use:reveal={{ delay: i * 80 }}
							class="reveal bg-muted border-border {s.accent} rounded-lg border border-l-[3px] p-3 text-xs leading-relaxed"
						>
							<div class="mb-1.5 flex items-center gap-2">
								<span class="text-base leading-none">{s.icon}</span>
								<span class="text-foreground font-medium">{s.name}</span>
							</div>
							<div class="mb-2">
								<code class="bg-card text-primary rounded-md px-1.5 py-0.5 font-mono text-xs">{s.q}</code>
							</div>
							<p class="text-muted-foreground">
								<span class="font-medium text-emerald-600 dark:text-emerald-300">Finds:</span>
								{s.finds}
							</p>
							<p class="text-muted-foreground mt-1">
								<span class="font-medium text-amber-600 dark:text-amber-300">Misses:</span>
								{s.misses}
							</p>
							<p class="text-muted-foreground mt-1">
								<span class="text-foreground font-medium">Use when:</span>
								{s.use}
							</p>
						</div>
					{/each}
				</div>
			</section>

			<!-- D · decision guide -->
			<section use:reveal class="reveal mb-10">
				<h2 class="text-foreground mb-1 text-lg font-semibold">Which search should I reach for?</h2>
				<p class="text-muted-foreground mb-4 text-sm leading-relaxed">
					Pick by what your memory actually held onto — the exact words, the gist, or the look.
				</p>
				<div class="border-border overflow-hidden rounded-lg border text-xs">
					{#each DECISION as r, i (r.when)}
						<div
							use:reveal={{ delay: i * 60 }}
							class="reveal grid grid-cols-[1fr_1.4fr] last:[&>div]:border-b-0"
						>
							<div class="border-border text-foreground border-b p-2 font-medium">{r.when}</div>
							<div class="border-border text-muted-foreground border-b border-l p-2">{r.use}</div>
						</div>
					{/each}
				</div>
			</section>

			<!-- 5 · per-mode pass table -->
			<section use:reveal class="reveal mb-10">
				<h2 class="text-foreground mb-3 text-lg font-semibold">How many passes each mode runs</h2>
				<div class="border-border overflow-hidden rounded-lg border text-xs">
					<div class="bg-muted text-foreground grid grid-cols-[1fr_auto_1.6fr] font-medium">
						<div class="border-border border-b p-2">Mode</div>
						<div class="border-border border-b border-l p-2 text-center">Passes</div>
						<div class="border-border border-b border-l p-2">What runs · fused by</div>
					</div>
					{#each MODES as m, i (m.mode)}
						<div
							use:reveal={{ delay: i * 60 }}
							class="reveal text-muted-foreground grid grid-cols-[1fr_auto_1.6fr] last:[&>div]:border-b-0 {m.mode === 'Image + text'
								? 'bg-highlight/10'
								: ''}"
						>
							<div class="border-border text-foreground border-b p-2 font-medium">{m.mode}</div>
							<div class="border-border border-b border-l p-2 text-center font-mono">
								{m.passes}
							</div>
							<div class="border-border border-b border-l p-2">
								{m.detail}{#if m.fuse !== '—'}&nbsp;· <span class="text-foreground">{m.fuse}</span>{/if}
							</div>
						</div>
					{/each}
				</div>
				<div
					use:reveal
					class="reveal text-muted-foreground mt-3 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-sm leading-relaxed"
				>
					⚠️ <strong class="text-foreground">Attaching an image overrides the dial.</strong> Image + text
					always becomes the 4-way <code class="font-mono">all</code> mode regardless of whether you picked
					Keyword, Vector, or Hybrid. Image alone stays single-pass Image; text alone obeys the dial.
				</div>
			</section>

			<!-- 5b · scenario matrix -->
			<section use:reveal class="reveal mb-10">
				<h2 class="text-foreground mb-1 text-lg font-semibold">
					Scenario matrix: exactly what runs, and when
				</h2>
				<p class="text-muted-foreground mb-3 text-sm leading-relaxed">
					Two levers decide everything. <strong class="text-foreground">(1)</strong> the dial (Keyword /
					Vector / Scene / Hybrid) only matters <em>when there's no image</em>.
					<strong class="text-foreground">(2)</strong> attaching an image is the master switch — image +
					any text becomes the 4-way <code class="font-mono">all</code> (dial &amp; slider ignored);
					image alone becomes
					<code class="font-mono">visual</code>. Inside any multi-pass mode
					<strong class="text-foreground">no leg is prioritised</strong> — fusion is equal-weight RRF (Hybrid
					may instead use the Balance slider).
				</p>

				<!-- interactive explorer: toggle and watch the passes light up / collapse -->
				<div class="bg-card border-border mb-3 rounded-xl border p-4">
					<div class="flex flex-wrap items-center gap-1.5">
						<span class="text-muted-foreground mr-1 text-xs">Dial</span>
						{#each DEMO_DIALS as d (d)}
							<button
								type="button"
								onclick={() => (dial = d)}
								class="rounded-md border px-2 py-1 text-xs font-medium capitalize transition-all {dial ===
								d
									? 'border-primary bg-primary/10 text-primary'
									: 'border-border text-muted-foreground hover:text-foreground'} {hasImage
									? 'opacity-40'
									: ''}">{d}</button
							>
						{/each}
						<span class="bg-border mx-1.5 h-4 w-px"></span>
						<button
							type="button"
							onclick={() => (hasText = !hasText)}
							class="rounded-md border px-2 py-1 text-xs font-medium transition-colors {hasText
								? 'border-foreground/40 text-foreground'
								: 'border-border text-muted-foreground'}">{hasText ? '✓ ' : ''}Text</button
						>
						<button
							type="button"
							onclick={() => (hasImage = !hasImage)}
							class="rounded-md border px-2 py-1 text-xs font-medium transition-colors {hasImage
								? 'border-amber-500/60 bg-amber-500/10 text-amber-600 dark:text-amber-300'
								: 'border-border text-muted-foreground'}">{hasImage ? '✓ ' : ''}🖼️ Image</button
						>
					</div>

					<div class="mt-3 flex flex-wrap items-center gap-2 text-xs">
						<span class="text-muted-foreground">→ runs</span>
						{#if demoMode}
							<code class="bg-primary/15 text-primary rounded-md px-2 py-0.5 font-mono font-semibold"
								>{demoMode}</code
							>
							<span class="text-muted-foreground"
								>{demoLegs.length} pass{demoLegs.length > 1 ? 'es' : ''}</span
							>
						{:else}
							<span class="text-muted-foreground italic">nothing — type text or attach an image</span>
						{/if}
					</div>

					<div class="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
						{#each DEMO_LANES as lane (lane.key)}
							{@const active = demoLegs.includes(lane.key)}
							<div
								class="rounded-lg border p-2.5 text-center transition-all duration-300 {active
									? lane.on
									: 'border-border text-muted-foreground scale-95 opacity-40 grayscale'}"
							>
								<div class="text-base leading-none">{lane.icon}</div>
								<div class="mt-1 text-xs font-medium">{lane.name}</div>
								<div class="text-[0.7rem] opacity-70">{lane.side}</div>
							</div>
						{/each}
					</div>

					<div class="mt-3 flex flex-wrap items-center gap-1.5 text-xs">
						<span
							class="rounded-md px-2 py-0.5 transition-colors {demoLegs.length > 1
								? 'bg-primary/10 text-primary'
								: 'bg-muted text-muted-foreground'}"
							>{demoLegs.length > 1
								? demoMode === 'hybrid'
									? 'fused: RRF / slider'
									: 'fused: equal RRF'
								: 'no fusion'}</span
						>
						<span
							class="rounded-md px-2 py-0.5 transition-all {demoSlider
								? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-300'
								: 'bg-muted text-muted-foreground line-through opacity-60'}">Balance slider</span
						>
						<span
							class="rounded-md px-2 py-0.5 transition-all {demoRerank
								? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-300'
								: 'bg-muted text-muted-foreground line-through opacity-60'}">Rerank</span
						>
					</div>

					<p class="text-muted-foreground mt-3 text-xs leading-relaxed">
						{#if hasImage && hasText}
							Image + text → the dial is <strong class="text-foreground">ignored</strong> and all four
							passes vote equally. Even with Dial = Vector, your text <em>still</em> runs the Keyword/BM25 leg
							— the image just adds a fourth voice (never a gate).
						{:else if hasImage}
							Image alone → a single <code class="font-mono">visual</code> pass; Rerank is a no-op without text.
						{:else if demoMode}
							No image → your dial picks the mode. Toggle <strong class="text-foreground">🖼️ Image</strong>
							and watch it collapse to the 4-way <code class="font-mono">all</code>.
						{:else}
							Add some text or an image to begin.
						{/if}
					</p>
				</div>

				<div class="text-muted-foreground mb-1.5 text-xs font-medium tracking-wide uppercase">
					Full reference
				</div>
				<div class="border-border overflow-x-auto rounded-lg border">
					<table class="w-full border-collapse text-xs">
						<thead>
							<tr class="bg-muted text-foreground text-left">
								<th class="border-border border-b p-2 font-medium whitespace-nowrap">You give</th>
								<th class="border-border border-b border-l p-2 font-medium">Mode</th>
								<th class="border-border border-b border-l p-2 font-medium">Passes that run</th>
								<th class="border-border border-b border-l p-2 font-medium">Combined by</th>
								<th class="border-border border-b border-l p-2 font-medium">Slider</th>
								<th class="border-border border-b border-l p-2 font-medium">Rerank</th>
							</tr>
						</thead>
						<tbody>
							{#each SCENARIOS as s (s.give)}
								<tr class="text-muted-foreground {s.highlight ? 'bg-highlight/10' : ''}">
									<td class="border-border text-foreground border-b p-2 font-medium whitespace-nowrap"
										>{s.give}</td
									>
									<td class="border-border border-b border-l p-2"
										><code class="text-primary font-mono">{s.mode}</code></td
									>
									<td class="border-border border-b border-l p-2">{s.passes}</td>
									<td class="border-border border-b border-l p-2">{s.combined}</td>
									<td class="border-border border-b border-l p-2">{s.slider}</td>
									<td class="border-border border-b border-l p-2 whitespace-nowrap">{s.rerank}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
				<div
					use:reveal
					class="reveal text-muted-foreground mt-3 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-sm leading-relaxed"
				>
					<strong class="text-foreground">What adding an image does:</strong> Keyword, Vector, Scene,
					<em>or</em>
					Hybrid all collapse to the same 4-way <code class="font-mono">all</code> the moment you attach
					one. So
					<strong class="text-foreground"
						>Vector + image is not "just the meaning &amp; image legs"</strong
					>
					—
					<code class="font-mono">all</code> runs <em>four equal legs</em>, so your text
					<em>also</em>
					drives a literal Keyword/BM25 leg and a Scene/caption leg. The dial you picked and the Balance slider
					stop mattering; the image is simply added as a fourth equal voice (never a gate). Image with no text
					stays a single <code class="font-mono">visual</code> pass.
				</div>
			</section>

			<!-- 5c · interactive worked examples -->
			<section use:reveal class="reveal mb-10">
				<h2 class="text-foreground mb-1 text-lg font-semibold">
					Worked examples: watch a full search run
				</h2>
				<p class="text-muted-foreground mb-3 text-sm leading-relaxed">
					Pick a real problem and step through exactly what each stage yields.
				</p>
				<div
					use:reveal
					class="reveal text-muted-foreground mb-3 rounded-lg border border-emerald-500/40 bg-emerald-500/5 p-3 text-xs leading-relaxed"
				>
					<span class="font-medium text-emerald-600 dark:text-emerald-300">✓ Verified live</span>
					against the running backend: Keyword, Vector, Hybrid, rerank, the Balance slider, Image (a posted
					frame self-matched at&nbsp;#1) and <code class="font-mono">all</code> all return real results;
					a name filter returned
					<strong class="text-foreground">15</strong> hits with prefilter vs
					<strong class="text-foreground">0</strong>
					with postfilter; fuzziness&nbsp;2 recovered a typo that fuzziness&nbsp;0 missed. The chunk IDs and
					cosines in the examples below are illustrative — the <em>mechanics</em> they show are what's verified.
				</div>
				<div class="mb-3 flex flex-wrap gap-1.5">
					{#each WORKED as w, i (w.key)}
						<button
							type="button"
							onclick={() => (exIdx = i)}
							class="rounded-md border px-2.5 py-1.5 text-xs font-medium transition-colors {exIdx ===
							i
								? 'border-primary bg-primary/10 text-primary'
								: 'border-border text-muted-foreground hover:text-foreground'}">{w.tab}</button
						>
					{/each}
				</div>

				{#key exIdx}
					{@const w = WORKED[exIdx]!}
					<div class="bg-card border-border rounded-xl border p-4">
						<div class="flex flex-wrap items-center gap-2">
							<span class="text-foreground text-sm font-semibold">{w.title}</span>
							<code
								class="bg-primary/15 text-primary rounded-md px-2 py-0.5 font-mono text-xs font-semibold"
								>mode: {w.mode}</code
							>
						</div>
						<p class="text-muted-foreground mt-1 text-xs leading-relaxed">{w.why}</p>

						<ol class="mt-4 space-y-3">
							{#each w.steps as s, i (s.stage)}
								<li use:reveal={{ delay: i * 70 }} class="reveal flex gap-3">
									<span
										class="bg-primary/15 text-primary grid size-6 shrink-0 place-items-center rounded-full text-xs font-semibold"
										>{i + 1}</span
									>
									<div class="min-w-0">
										<div class="text-foreground text-xs font-semibold">{s.stage}</div>
										<p
											class="text-muted-foreground mt-0.5 text-xs leading-relaxed [overflow-wrap:anywhere]"
										>
											{s.yields}
										</p>
									</div>
								</li>
							{/each}
						</ol>

						<div
							use:reveal
							class="reveal border-primary/40 bg-primary/5 mt-4 rounded-lg border p-3 text-xs leading-relaxed"
						>
							<span class="text-foreground font-semibold">Result:</span>
							<span class="text-muted-foreground">{w.result}</span>
						</div>
						<div
							use:reveal
							class="reveal bg-muted border-border mt-2 rounded-lg border p-3 text-xs leading-relaxed"
						>
							<span class="text-foreground font-semibold">Takeaway:</span>
							<span class="text-muted-foreground">{w.takeaway}</span>
						</div>
					</div>
				{/key}
			</section>

			<!-- 6 · the pipeline -->
			<section use:reveal class="reveal mb-10">
				<h2 class="text-foreground mb-4 text-lg font-semibold">The whole pipeline</h2>
				<div class="flex flex-col items-center">
					<div class="grid w-full grid-cols-3 gap-2">
						<div
							class="bg-muted border-border rounded-md border border-l-[3px] border-l-sky-400 px-2 py-2 text-center text-xs"
						>
							⌨️ Keyword text
						</div>
						<div
							class="bg-muted border-border border-l-primary rounded-md border border-l-[3px] px-2 py-2 text-center text-xs"
						>
							💬 Meaning text
						</div>
						<div
							class="bg-muted border-border rounded-md border border-l-[3px] border-l-amber-400 px-2 py-2 text-center text-xs"
						>
							🖼️ Image
						</div>
					</div>
					<div class="text-muted-foreground grid w-full grid-cols-3 text-center text-[0.7rem]">
						<span>▼</span><span>▼ embed</span><span>▼ embed</span>
					</div>
					<div class="grid w-full grid-cols-3 gap-2">
						<div class="bg-background border-border rounded-md border px-2 py-1.5 text-center text-xs">
							FTS leg
							<div class="text-muted-foreground">BM25 on text</div>
						</div>
						<div class="bg-background border-border rounded-md border px-2 py-1.5 text-center text-xs">
							Text-vector leg
							<div class="text-muted-foreground">text_embedding</div>
						</div>
						<div class="bg-background border-border rounded-md border px-2 py-1.5 text-center text-xs">
							Frame-vector leg
							<div class="text-muted-foreground">frame_embedding</div>
						</div>
					</div>
					<p class="text-muted-foreground mt-2 text-center text-xs">
						+ the text query also drives a <strong class="text-foreground">Scene</strong> leg (<code
							class="font-mono">caption_embedding</code
						>) in <code class="font-mono">all</code>.
					</p>

					<div class="flow-line"><span class="flow-dot"></span></div>

					<div
						class="border-primary/50 bg-primary/10 text-foreground w-full rounded-lg border px-4 py-2.5 text-center text-sm font-medium"
					>
						Fuse into one ranking — <span class="font-mono">RRF</span> (default) or Balance slider (hybrid only)
					</div>

					<div class="flow-line"><span class="flow-dot" style="animation-delay:.3s"></span></div>

					<div class="bg-muted border-border w-full rounded-md border px-4 py-2 text-center text-sm">
						Rerank top <em>K</em> <span class="text-muted-foreground">(optional)</span> — re-read each transcript
						vs your text
					</div>

					<div class="flow-line"><span class="flow-dot" style="animation-delay:.6s"></span></div>

					<div
						class="bg-card border-border text-foreground w-full rounded-lg border px-4 py-2.5 text-center font-semibold"
					>
						Top N ranked chunk results
					</div>
				</div>
			</section>

			<!-- 7 · RRF worked example -->
			<section use:reveal class="reveal mb-10">
				<h2 class="text-foreground mb-1 text-lg font-semibold">
					How the lists merge: RRF (worked example)
				</h2>
				<p class="text-muted-foreground mb-3 text-sm leading-relaxed">
					Think of every active signal as a judge handing in a ranked shortlist. The clip that lands high
					on the
					<strong class="text-foreground">most</strong> shortlists wins — even if it's nobody's outright #1.
					Here it is with two judges on 5 clips (A–E), each returning a ranked list:
				</p>
				<div class="grid gap-3 sm:grid-cols-2">
					<div use:reveal class="reveal bg-muted border-border rounded-lg border p-3 text-xs">
						<div class="text-foreground mb-1 font-medium">⌨️ Keyword</div>
						<div class="text-muted-foreground">
							{#each keywordRanked as c, i (c)}<span class="text-foreground font-mono">{i + 1} = {c}</span>{#if i < keywordRanked.length - 1}&ensp;·&ensp;{/if}{/each}
						</div>
					</div>
					<div
						use:reveal={{ delay: 90 }}
						class="reveal bg-muted border-border rounded-lg border p-3 text-xs"
					>
						<div class="text-foreground mb-1 font-medium">💬 Meaning</div>
						<div class="text-muted-foreground">
							{#each meaningRanked as c, i (c)}<span class="text-foreground font-mono">{i + 1} = {c}</span>{#if i < meaningRanked.length - 1}&ensp;·&ensp;{/if}{/each}
						</div>
					</div>
				</div>
				<p class="text-muted-foreground mt-3 mb-2 text-sm">
					Each clip scores <code class="text-primary bg-muted rounded-md px-1 font-mono text-xs"
						>Σ 1/(60 + rank)</code
					>
					over the lists it appears in (rank counts from 0, so the #1 clip uses 1/60):
				</p>
				<div class="bg-card border-border overflow-hidden rounded-lg border">
					{#each rrfRows as row, i (row.clip)}
						<div
							use:reveal={{ delay: i * 100 }}
							class="reveal border-border flex items-center gap-3 border-b px-3 py-2 text-xs last:border-b-0 {row.inBoth
								? 'bg-highlight/10'
								: ''}"
						>
							<span class="text-foreground w-6 font-mono font-medium">{row.clip}</span>
							<span class="text-muted-foreground flex-1 font-mono">
								{row.parts.map((p) => `1/${K + p.rank}`).join(' + ')}
							</span>
							<span class="text-foreground font-mono">≈ {row.score.toFixed(4)}</span>
							{#if row.inBoth}
								<span
									class="bg-highlight/30 text-foreground rounded-md px-1.5 py-0.5 text-[0.7rem] font-medium"
									>in both → wins</span
								>
							{:else}
								<span class="text-muted-foreground text-[0.7rem]">one list</span>
							{/if}
						</div>
					{/each}
				</div>
				<p class="text-muted-foreground mt-3 text-sm leading-relaxed">
					Clips that appear in <strong class="text-foreground">more lists</strong> rise to the top. RRF
					needs no tuning and works the same for
					<strong class="text-foreground">2, 3, or 4 lists</strong>
					— you just add another <code class="font-mono">1/(60 + rank)</code> term. The multi-judge
					<code class="font-mono">all</code> mode <em>always</em> uses equal-weight RRF.
				</p>

				<div
					use:reveal
					class="reveal border-border bg-card text-muted-foreground mt-3 rounded-lg border p-3 text-xs leading-relaxed"
				>
					<div class="text-foreground mb-1 font-medium">
						How the merge actually works (and why any number of legs is fine)
					</div>
					The legs run<strong class="text-foreground">independently</strong> — none feeds its hits to
					another (the image's results are <em>not</em> handed to the Vector leg). Each returns its
					<em>own</em>
					ranked list over the whole library. RRF then pours all the lists into
					<strong class="text-foreground">one pot keyed by chunk</strong>: a chunk's score is the
					<strong class="text-foreground">sum</strong>
					of
					<code class="font-mono">1/(60 + rank)</code> from <em>every</em> list it appears in (absent
					from a list → adds 0). So it's a
					<strong class="text-foreground">union with added scores</strong>
					— not a chain, and not a plain concatenation. Because it's just a sum, it extends to
					<strong class="text-foreground">any number of lists</strong>
					(2 for Hybrid, up to 4 for <code class="font-mono">all</code>) — you add one more term per leg.
					A chunk only one leg found still shows up (it just scores low); a chunk several legs rank
					highly wins.
				</div>
			</section>

			<!-- 8 · slider vs RRF -->
			<section use:reveal class="reveal mb-10">
				<h2 class="text-foreground mb-1 text-lg font-semibold">
					Balance slider vs RRF — the key difference
				</h2>
				<p class="text-muted-foreground mb-3 text-sm leading-relaxed">
					The Balance slider is a <strong class="text-foreground">2-way blend</strong> of the actual
					scores; it only exists for Hybrid (keyword ↔ meaning). It cannot describe 3 legs, so the moment
					you add an image, fusion falls back to equal-weight RRF and the slider is
					<strong class="text-foreground">ignored</strong>.
				</p>
				<div
					use:reveal
					class="reveal bg-muted border-border mb-3 rounded-lg border p-3 text-xs leading-relaxed"
				>
					<span class="text-foreground font-medium">For instance:</span> searching
					<code class="bg-card text-primary rounded-md px-1 font-mono">elcertifikat</code> → slide toward
					<strong class="text-foreground">Keyword</strong> for that exact word; searching
					<code class="bg-card text-primary rounded-md px-1 font-mono"
						>vad regeringen gör åt höga elpriser</code
					>
					→ slide toward <strong class="text-foreground">Vector</strong> to catch a clip that says
					"stötta hushållen med elkostnaderna". The slider only re-weights the two <em>text</em> passes — a
					weighted blend of their scores, not rank-voting — and does nothing for Scene or Image.
				</div>
				<div class="grid gap-3 sm:grid-cols-2">
					<div use:reveal class="reveal bg-muted border-border rounded-lg border p-3 text-xs">
						<div class="text-foreground font-medium">Slider (2 judges only)</div>
						<div class="text-muted-foreground mt-0.5">Hybrid keyword ↔ meaning.</div>
						<div class="text-foreground mt-2 font-mono">w·vectorScore + (1−w)·ftsScore</div>
						<div class="text-muted-foreground mt-0.5">Uses real scores. You pick the weight.</div>
					</div>
					<div
						use:reveal={{ delay: 90 }}
						class="reveal bg-muted border-border rounded-lg border p-3 text-xs"
					>
						<div class="text-foreground font-medium">RRF (2, 3 or 4 judges)</div>
						<div class="text-muted-foreground mt-0.5">
							Default everywhere; always for multi-leg "all".
						</div>
						<div class="text-foreground mt-2 font-mono">Σ 1/(60 + rank)</div>
						<div class="text-muted-foreground mt-0.5">Uses ranks only. Equal weight, no tuning.</div>
					</div>
				</div>
			</section>

			<!-- 9 · rerank -->
			<section use:reveal class="reveal bg-card border-border mb-10 rounded-lg border p-4">
				<h2 class="text-foreground mb-1 text-lg font-semibold">
					Rerank — what it does, and which column it reads
				</h2>
				<p class="text-muted-foreground mb-3 text-sm leading-relaxed">
					A second, slower-but-sharper precision pass. A cross-encoder reads your query text and one
					chunk's transcript <em>together</em> and scores how well they actually answer each other — better
					than the first-stage ranking, so it only runs on the head.
				</p>
				<div class="border-border overflow-hidden rounded-lg border text-xs">
					<div class="text-muted-foreground grid grid-cols-[7.5rem_1fr]">
						<div class="border-border text-foreground border-b p-2 font-medium">Reads</div>
						<div class="border-border border-b border-l p-2">
							one column — each candidate's transcript, the <code
								class="bg-muted text-primary rounded-md px-1 font-mono">text</code
							> field on the chunk. Nothing else.
						</div>
					</div>
					<div class="text-muted-foreground grid grid-cols-[7.5rem_1fr]">
						<div class="border-border text-foreground border-b p-2 font-medium">Scores against</div>
						<div class="border-border border-b border-l p-2">
							your <code class="font-mono">Keyword + Meaning</code> text joined (<code class="font-mono"
								>q + q_vec</code
							>).
						</div>
					</div>
					<div class="text-muted-foreground grid grid-cols-[7.5rem_1fr]">
						<div class="border-border text-foreground border-b p-2 font-medium">Scope</div>
						<div class="border-border border-b border-l p-2">
							the top <strong class="text-foreground">K</strong> (default 20) of the fused list; it reorders
							just that head. Everything below K keeps its first-stage order — no new search runs.
						</div>
					</div>
					<div class="text-muted-foreground grid grid-cols-[7.5rem_1fr]">
						<div class="text-foreground p-2 font-medium">Ignores</div>
						<div class="border-border border-l p-2">
							the image, the frame <code class="font-mono">caption</code>, and
							<strong class="text-foreground">all</strong>
							vectors. So an image-only search (no query text) → rerank is a no-op.
						</div>
					</div>
				</div>
				<div
					use:reveal
					class="reveal bg-muted border-border mt-3 rounded-lg border p-3 text-xs leading-relaxed"
				>
					<span class="text-foreground font-medium">Before → after</span>, query
					<code class="bg-card text-primary rounded-md px-1 font-mono">höjda räntor</code>: the fused
					top-3 might be ① a "minister vid podium" clip that drifted in on Vector, ② a stakeout that says
					"räntan" once, ③ the clip that actually says "vi måste hantera de höjda räntorna". The
					cross-encoder reads only each transcript and reorders to
					<strong class="text-foreground">③ ① ②</strong> — promoting the clip whose spoken words really match.
				</div>
			</section>

			<!-- 10 · the one real prefilter: Filters -->
			<section use:reveal class="reveal border-border bg-muted mb-10 rounded-lg border p-4">
				<h2 class="text-foreground mb-2 text-lg font-semibold">The one real "narrow-first": Filters</h2>
				<p class="text-muted-foreground text-sm leading-relaxed">
					The <strong class="text-foreground">Filters</strong> panel (language, name, referenskod,
					extraid, raw SQL) is the only thing that narrows the library — and it has nothing to do with
					the image. A filter becomes a <code class="font-mono">WHERE</code> clause. For the
					<strong class="text-foreground">transcript legs</strong> (Keyword, Vector, Hybrid),
					<em>prefilter</em>
					narrows the corpus before they search (off → after); the
					<strong class="text-foreground">frame legs</strong>
					(Image, Scene) always filter after ranking. So
					<strong class="text-foreground">Filter = a fence around the whole library</strong>; an attached
					image = just one more voice in the merge.
				</p>

				<div use:reveal class="reveal border-border mt-3 overflow-hidden rounded-lg border text-xs">
					<div class="bg-muted text-foreground grid grid-cols-[1fr_1.5fr] font-medium">
						<div class="border-border border-b p-2">Filter</div>
						<div class="border-border border-b border-l p-2">
							What it targets (all on the chunk's metadata)
						</div>
					</div>
					<div class="text-muted-foreground grid grid-cols-[1fr_1.5fr]">
						<div class="border-border text-foreground border-b p-2">Language</div>
						<div class="border-border border-b border-l p-2">
							<code class="font-mono">language = …</code> — exact match
						</div>
					</div>
					<div class="text-muted-foreground grid grid-cols-[1fr_1.5fr]">
						<div class="border-border text-foreground border-b p-2">Name (namn)</div>
						<div class="border-border border-b border-l p-2">
							<code class="font-mono">namn LIKE '%…%'</code> — substring match
						</div>
					</div>
					<div class="text-muted-foreground grid grid-cols-[1fr_1.5fr]">
						<div class="border-border text-foreground border-b p-2">Ref (referenskod)</div>
						<div class="border-border border-b border-l p-2">
							<code class="font-mono">referenskod LIKE '%…%'</code> — substring match
						</div>
					</div>
					<div class="text-muted-foreground grid grid-cols-[1fr_1.5fr]">
						<div class="border-border text-foreground border-b p-2">Internal ID (extraid)</div>
						<div class="border-border border-b border-l p-2">
							<code class="font-mono">extraid = …</code> — exact match
						</div>
					</div>
					<div class="text-muted-foreground grid grid-cols-[1fr_1.5fr]">
						<div class="border-border text-foreground p-2">Raw SQL</div>
						<div class="border-border border-l p-2">
							any expression over chunk columns, ANDed in verbatim
						</div>
					</div>
				</div>
				<p use:reveal class="reveal text-muted-foreground mt-2 text-xs leading-relaxed">
					All conditions are <strong class="text-foreground">ANDed</strong> together and target the
					<code class="font-mono">chunks</code> table's metadata — <em>not</em> frame attributes — so
					even Image and Scene results are filtered by <em>their chunk's</em> language / name / ref / id.
				</p>

				<div
					use:reveal
					class="reveal bg-card border-border mt-3 rounded-lg border p-3 text-xs leading-relaxed"
				>
					<span class="text-foreground font-medium">For instance:</span> search
					<code class="bg-muted text-primary rounded-md px-1 font-mono">nya regler</code> with a filter
					<code class="bg-muted rounded-md px-1 font-mono">namn = "Andersson"</code> and N = 10.
					<strong class="text-foreground">Prefilter (on):</strong> every leg that runs (here Keyword +
					Vector) searches <em>only</em> Andersson's chunks → a full 10 ranked Andersson hits.
					<strong class="text-foreground">Postfilter (off):</strong> each leg first finds its top 10
					across
					<em>all</em> ministers, then drops the non-Andersson ones — if only 3 were Andersson, you see 3.
					A tight postfilter can come up short.
				</div>
			</section>

			<!-- E · reading & troubleshooting results -->
			<section use:reveal class="reveal mb-10">
				<h2 class="text-foreground mb-1 text-lg font-semibold">Reading &amp; fixing your results</h2>
				<p class="text-muted-foreground mb-4 text-sm leading-relaxed">
					Every hit won a popularity contest among the signals you turned on. Read a surprising result by
					asking "which side did it win on?", and fix a bad list by changing <em>who votes</em>, not by
					staring harder. Mental dial:
					<strong class="text-foreground">recall vs precision</strong> — widen when you're missing things,
					tighten when the top is junk.
				</p>
				<div class="border-border overflow-hidden rounded-lg border text-xs">
					{#each TROUBLESHOOT as r, i (r.symptom)}
						<div
							use:reveal={{ delay: i * 50 }}
							class="reveal grid grid-cols-[1fr_1.5fr] last:[&>div]:border-b-0"
						>
							<div class="border-border text-foreground border-b p-2 font-medium">{r.symptom}</div>
							<div class="border-border text-muted-foreground border-b border-l p-2">{r.fix}</div>
						</div>
					{/each}
				</div>
			</section>

			<!-- F · how information flows -->
			<section use:reveal class="reveal mb-10">
				<h2 class="text-foreground mb-1 text-lg font-semibold">Under the hood: how a query travels</h2>
				<p class="text-muted-foreground mb-4 text-sm leading-relaxed">
					What actually happens between hitting Enter and seeing results.
				</p>
				<ol class="space-y-3">
					{#each JOURNEY as s, i (s.label)}
						<li use:reveal={{ delay: i * 60 }} class="reveal flex gap-3">
							<span
								class="bg-primary text-primary-foreground grid size-6 shrink-0 place-items-center rounded-full text-xs font-semibold"
								>{i + 1}</span
							>
							<div>
								<div class="text-foreground text-sm font-medium">{s.label}</div>
								<p class="text-muted-foreground mt-0.5 text-xs leading-relaxed">{s.detail}</p>
							</div>
						</li>
					{/each}
				</ol>

				<div class="mt-4 grid gap-3 sm:grid-cols-2">
					<div
						use:reveal
						class="reveal bg-muted border-border rounded-lg border p-3 text-xs leading-relaxed"
					>
						<div class="text-foreground font-medium">Two tables, one key</div>
						<p class="text-muted-foreground mt-1">
							<code class="font-mono">chunks</code> holds the transcript side;
							<code class="font-mono">chunk_frames</code>
							holds one row per video frame (its caption + image vector). They're linked by
							<code class="font-mono">(doc_id, speech_id, chunk_id)</code>. Image / Scene search the frames
							table, then join back — which is why every result is a <em>chunk</em>, even when the match
							was visual.
						</p>
					</div>
					<div
						use:reveal={{ delay: 90 }}
						class="reveal bg-muted border-border rounded-lg border p-3 text-xs leading-relaxed"
					>
						<div class="text-foreground font-medium">One shared embedding space</div>
						<p class="text-muted-foreground mt-1">
							Transcript and caption vectors live in the <em>same</em> space, so a single query-text vector can
							be compared to both (Vector → transcripts, Scene → captions). Your image vector lives with the
							frame vectors (Image). Same map, different pins.
						</p>
					</div>
					<div
						use:reveal={{ delay: 180 }}
						class="reveal bg-muted border-border rounded-lg border p-3 text-xs leading-relaxed sm:col-span-2"
					>
						<div class="text-foreground font-medium">Where the work happens</div>
						<p class="text-muted-foreground mt-1">
							The <strong class="text-foreground">frontend</strong> (this UI) talks to the
							<strong class="text-foreground">backend</strong>, which talks to two separate model servers —
							one for
							<strong class="text-foreground">embeddings</strong>, one for
							<strong class="text-foreground">reranking</strong> — and reads the LanceDB tables on disk. If
							the embedding server is down a vector search fails fast with a clear error; if an embedding
							column was never built, that leg returns nothing in a fused search and the others carry on (a
							single Vector or Hybrid search instead shows a clear "not built" message). The
							<strong class="text-foreground">Services</strong> badge in the sidebar shows their live status.
						</p>
					</div>
				</div>
			</section>

			<!-- 11 · settings reference -->
			<section use:reveal class="reveal mb-10">
				<h2 class="text-foreground mb-3 text-lg font-semibold">⚙️ Settings reference</h2>
				<div class="border-border overflow-hidden rounded-lg border text-xs">
					{#each SETTINGS as s, i (s.name)}
						<div
							use:reveal={{ delay: i * 70 }}
							class="reveal text-muted-foreground grid grid-cols-[1fr_1.7fr] last:[&>div]:border-b-0"
						>
							<div class="border-border text-foreground border-b p-2">{s.name}</div>
							<div class="border-border border-b border-l p-2">{s.desc}</div>
						</div>
					{/each}
				</div>
			</section>

			<!-- 12 · limitations -->
			<section use:reveal class="reveal bg-muted border-border mb-8 rounded-lg border p-4">
				<h2 class="text-foreground mb-2 text-lg font-semibold">Known limitations</h2>
				<ul class="text-muted-foreground list-disc space-y-1 pl-5 text-sm leading-relaxed">
					{#each LIMITS as l (l)}<li>{l}</li>{/each}
				</ul>
			</section>

			<!-- G · glossary -->
			<section use:reveal class="reveal mb-10">
				<h2 class="text-foreground mb-1 text-lg font-semibold">Glossary — the words you'll see</h2>
				<p class="text-muted-foreground mb-4 text-sm leading-relaxed">
					Each term in one line. None of them are magic — each is a librarian with one specific talent.
				</p>
				<dl class="border-border divide-border divide-y overflow-hidden rounded-lg border text-xs">
					{#each GLOSSARY as g, i (g.term)}
						<div
							use:reveal={{ delay: i * 40 }}
							class="reveal grid grid-cols-[8.5rem_1fr] gap-2 p-2"
						>
							<dt class="text-foreground font-medium">{g.term}</dt>
							<dd class="text-muted-foreground">{g.def}</dd>
						</div>
					{/each}
				</dl>
			</section>

			<a
				use:reveal
				href="{base}/"
				class="reveal border-primary/40 bg-primary/10 text-foreground hover:bg-primary/15 group flex items-center gap-2 rounded-lg border px-4 py-3 text-sm font-medium transition-colors"
			>
				Try it on the Search page
				<ArrowRight class="size-4 transition-transform group-hover:translate-x-0.5" />
			</a>
		{:else}
			<!-- ░░ ATLAS ░░ -->
			<p use:reveal class="reveal text-muted-foreground mb-6 text-sm leading-relaxed">
				The Atlas is a <strong class="text-foreground">2-D map of every chunk</strong>, laid out by the
				<em>meaning of its transcript text</em> — an EVōC projection of the same text embeddings the Meaning
				judge uses. It's a bird's-eye view of the whole corpus.
			</p>

			<div class="grid gap-3 sm:grid-cols-2">
				<div
					use:reveal
					class="reveal bg-muted border-border border-l-primary rounded-lg border border-l-[3px] p-3"
				>
					<div class="text-foreground font-medium">What it shows</div>
					<p class="text-muted-foreground mt-1 text-xs leading-relaxed">
						Each dot is one chunk. Chunks with similar wording sit close together; colour encodes the EVōC
						cluster they fall into. Dense regions are recurring themes across press conferences.
					</p>
				</div>
				<div
					use:reveal={{ delay: 90 }}
					class="reveal bg-muted border-border rounded-lg border border-l-[3px] border-l-emerald-400 p-3"
				>
					<div class="text-foreground font-medium">How to read it</div>
					<p class="text-muted-foreground mt-1 text-xs leading-relaxed">
						Distance ≈ semantic similarity. Two clips far apart talk about different things even if they
						share a word; two clips close together are about the same thing even in different words.
					</p>
				</div>
				<div
					use:reveal={{ delay: 180 }}
					class="reveal bg-muted border-border rounded-lg border border-l-[3px] border-l-sky-400 p-3"
				>
					<div class="text-foreground font-medium">How to use it</div>
					<p class="text-muted-foreground mt-1 text-xs leading-relaxed">
						Pan and zoom, hover a point for its transcript, or lasso a region to cross-filter the linked
						charts and table. Great for discovering clusters you'd never have searched for by keyword.
					</p>
				</div>
				<div
					use:reveal={{ delay: 270 }}
					class="reveal bg-muted border-border rounded-lg border border-l-[3px] border-l-amber-400 p-3"
				>
					<div class="text-foreground font-medium">Build / rebuild it</div>
					<p class="text-muted-foreground mt-1 text-xs leading-relaxed">
						The projection is computed offline. Run
						<code class="bg-card rounded-md px-1 py-0.5 font-mono text-xs">ratch feature atlas</code>
						to (re)generate it; the tab shows a prompt until it exists.
					</p>
				</div>
			</div>

			<div
				use:reveal
				class="reveal border-border bg-card text-muted-foreground mt-4 rounded-lg border p-3 text-sm leading-relaxed"
			>
				The Atlas maps <strong class="text-foreground">transcript meaning only</strong> — not the video frames
				or captions. It complements Search: Search answers "where is X?", the Atlas answers "what's in here,
				and how does it group?"
			</div>

			<a
				use:reveal
				href="{base}/atlas"
				class="reveal border-primary/40 bg-primary/10 text-foreground hover:bg-primary/15 group mt-6 flex items-center gap-2 rounded-lg border px-4 py-3 text-sm font-medium transition-colors"
			>
				Open the Atlas
				<ArrowRight class="size-4 transition-transform group-hover:translate-x-0.5" />
			</a>
		{/if}
	</div>
</div>

<style>
	/* scroll-reveal: hidden base in scoped CSS; `.in-view` added by JS.
   * `:global(.in-view)` stops Svelte pruning the compile-time-unused class. */
	.reveal {
		opacity: 0;
		transform: translateY(14px);
		transition:
			opacity 0.55s ease,
			transform 0.55s ease;
	}
	.reveal:global(.in-view) {
		opacity: 1;
		transform: none;
	}

	/* lane "scan" bar — all lanes share one timing (no stagger) so the sweep
   * reads as "all four searches run at the same time". */
	.lane-scan {
		position: absolute;
		bottom: 0;
		left: 0;
		height: 2px;
		width: 100%;
		transform: scaleX(0);
		transform-origin: left;
		opacity: 0.7;
		animation: scan 2.2s ease-in-out infinite;
	}
	@keyframes scan {
		0% {
			transform: scaleX(0);
		}
		50% {
			transform: scaleX(1);
		}
		100% {
			transform: scaleX(0);
			transform-origin: right;
		}
	}

	/* vertical connector with a dot flowing down = "data moving through" */
	.flow-line {
		position: relative;
		width: 2px;
		height: 30px;
		margin: 4px auto;
		background: var(--color-border);
	}
	.flow-dot {
		position: absolute;
		left: 50%;
		width: 7px;
		height: 7px;
		border-radius: 9999px;
		background: var(--color-primary);
		box-shadow: 0 0 8px 1px var(--color-primary);
		translate: -50% 0;
		animation: flow 1.8s ease-in-out infinite;
	}
	@keyframes flow {
		0% {
			top: -4px;
			opacity: 0;
		}
		20% {
			opacity: 1;
		}
		80% {
			opacity: 1;
		}
		100% {
			top: 28px;
			opacity: 0;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.reveal {
			opacity: 1;
			transform: none;
			transition: none;
		}
		.flow-dot,
		.lane-scan {
			display: none;
		}
	}
</style>
