<script lang="ts">
	import type { Alignment } from '@rask/media-api';
	import { queryTerms } from '$lib/utils';
	import { on } from 'svelte/events';

	type Props = {
		alignments: Alignment[];
		/** Live media element. We read `currentTime` and listen for `seeked`. */
		media: HTMLMediaElement | null;
		query?: string;
		/**
		 * Render the standalone box chrome (border + surface background). Set to
		 * `false` when an outer wrapper already provides the frame/scroll (e.g. the
		 * unified player card or the fullscreen transcript overlay), so this doesn't
		 * draw a nested box and its opaque background doesn't defeat the scrim.
		 */
		chrome?: boolean;
	};
	let { alignments, media, query = '', chrome = true }: Props = $props();

	const terms = $derived(new Set(queryTerms(query)));

	/** Strip leading/trailing punctuation, lowercase. */
	const normalize = (w: string) => w.replace(/^\W+|\W+$/gu, '').toLowerCase();

	let scrollContainer = $state<HTMLDivElement | null>(null);

	/**
	 * Karaoke cursor.
	 *
	 * The DOM (`scrollContainer`) and the data (`alignments`) are independent
	 * reactive sources, so we drive the RAF loop from `$effect`. Whenever
	 * either changes, the previous RAF + listeners are torn down via the
	 * cleanup return, then the wordMap/sentMap are rebuilt against the
	 * current DOM. This was the bug in my first port — `{@attach}` only
	 * runs once per parent <div> mount, but the inner spans re-render on
	 * every hit change, so the captured maps went stale.
	 */
	$effect(() => {
		if (!scrollContainer || !media) return;
		// Snapshot the element for this run. `media` is a prop (a live getter), so
		// reading it in cleanup could see a newer/null value and detach from the
		// wrong element (or null-deref). Capture once → add, remove, and the RAF
		// tick all act on the same element. Mirrors player-pane's `const el`.
		const el = media;

		type Ref = { el: HTMLElement; start: number; end: number };
		const wordMap: Ref[] = [];
		const sentMap: Ref[] = [];

		// Touch `alignments` so the effect re-runs when a new hit is selected
		// and the spans below have been replaced by Svelte's reconciler.
		void alignments;

		for (const el of scrollContainer.querySelectorAll<HTMLElement>('[data-word]')) {
			wordMap.push({
				el,
				start: parseFloat(el.dataset.start ?? '0'),
				end: parseFloat(el.dataset.end ?? '0'),
			});
		}
		for (const el of scrollContainer.querySelectorAll<HTMLElement>('[data-sentence]')) {
			sentMap.push({
				el,
				start: parseFloat(el.dataset.start ?? '0'),
				end: parseFloat(el.dataset.end ?? '0'),
			});
		}

		function find(segs: Ref[], t: number): Ref | null {
			let lo = 0,
				hi = segs.length - 1;
			while (lo <= hi) {
				const mid = (lo + hi) >> 1;
				const seg = segs[mid];
				if (seg === undefined) break;
				if (t < seg.start) hi = mid - 1;
				else if (t >= seg.end) lo = mid + 1;
				else return seg;
			}
			return null;
		}

		let prevWord: HTMLElement | null = null;
		let prevSent: HTMLElement | null = null;

		function refresh() {
			const t = el.currentTime;
			const w = find(wordMap, t);
			if (w !== null && w.el !== prevWord) {
				prevWord?.classList.remove('cursor-word');
				w.el.classList.add('cursor-word');
				prevWord = w.el;
			}
			const s = find(sentMap, t);
			if (s !== null && s.el !== prevSent) {
				prevSent?.classList.remove('cursor-sentence');
				s.el.classList.add('cursor-sentence');
				s.el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
				prevSent = s.el;
			}
		}

		// `timeupdate` fires ~4–66×/s during playback (enough for word-level
		// karaoke) and costs NOTHING when paused, while `seeked` covers dragging
		// the playhead on a paused video. This replaces the old always-on
		// requestAnimationFrame(tick) loop — up to 3 windowed highlighters share
		// one media element, so the RAF loops were 3 idle 60fps spinners on a
		// paused video for zero benefit over these two listeners.
		const offSeeked = on(el, 'seeked', refresh);
		const offTimeUpdate = on(el, 'timeupdate', refresh);
		refresh();

		return () => {
			offSeeked();
			offTimeUpdate();
			prevWord?.classList.remove('cursor-word');
			prevSent?.classList.remove('cursor-sentence');
		};
	});
</script>

<div
	bind:this={scrollContainer}
	class={chrome ? 'border-border bg-muted rounded-md border p-3 text-sm leading-7' : 'p-3'}
>
	{#each alignments as a (a.start)}
		{@const sentEndsWithSpace = (a.text ?? '').endsWith(' ')}
		<span
			data-sentence
			data-start={a.start}
			data-end={a.end}
			class="hover:bg-muted/60 cursor-pointer rounded-sm transition-colors"
			onclick={() => {
	if (media) {
		media.currentTime = a.start;
		media.play().catch(() => {});
	}
}}
			onkeydown={(e) => {
	if (e.key === 'Enter' || e.key === ' ') {
		e.preventDefault();
		if (media) {
			media.currentTime = a.start;
			media.play().catch(() => {});
		}
	}
}}
			role="button"
			tabindex="0"
		>
			{#each a.words ?? [] as w (w.start + ':' + w.text)}
				{@const stripped = normalize(w.text ?? '')}
				<span
					data-word
					data-start={w.start}
					data-end={w.end}
					class="rounded-sm"
					class:underline={terms.has(stripped)}
					class:decoration-highlight={terms.has(stripped)}
					class:decoration-2={terms.has(stripped)}
					class:underline-offset-2={terms.has(stripped)}>{w.text}</span
				>
			{/each}
		</span>
		{#if !sentEndsWithSpace}<br />{/if}
	{/each}
</div>
