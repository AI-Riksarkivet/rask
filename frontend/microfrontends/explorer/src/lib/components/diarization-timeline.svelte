<script lang="ts">
	import type { DiarTurn } from '@rask/explorer-api';
	import { fmtTime } from '$lib/utils';
	import { AudioLines } from '@lucide/svelte';

	/** "Find this voice" pick: a whole lane (per-video speaker centroid) or one
	 *  diarized turn (right-click on a segment). The host owns doc_id and turns
	 *  this into a `VoiceAnchor` — the timeline never navigates itself. */
	type VoicePick = { speaker: string } | { turnId: number };

	type Props = {
		turns: DiarTurn[];
		currentTime: number;
		duration: number;
		/** Current chunk's [start,end]; when scope='chunk' the timeline zooms to it. */
		chunkStart?: number;
		chunkEnd?: number;
		onSeek: (t: number) => void;
		/** Gates the voice affordances on GET /api/voice/status `built`. */
		voiceEnabled?: boolean;
		onFindVoice?: (pick: VoicePick) => void;
	};
	let {
		turns,
		currentTime,
		duration,
		chunkStart,
		chunkEnd,
		onSeek,
		voiceEnabled = false,
		onFindVoice,
	}: Props = $props();

	const showVoice = $derived(voiceEnabled && onFindVoice !== undefined);

	// Zoom: 'chunk' = just the playing chunk (compact — a chunk has 1–3 speakers);
	// 'video' = the whole recording (every speaker).
	let scope = $state<'chunk' | 'video'>('chunk');
	const canChunk = $derived(chunkStart != null && chunkEnd != null && chunkEnd > chunkStart);

	const win = $derived.by(() => {
		if (scope === 'chunk') {
			// Same condition as canChunk, restated so TS narrows via locals (no `as`).
			const lo = chunkStart;
			const hi = chunkEnd;
			if (lo != null && hi != null && hi > lo) return { lo, hi };
		}
		return { lo: 0, hi: Math.max(duration, 0.001) };
	});
	const span = $derived(Math.max(0.001, win.hi - win.lo));

	// Every distinct speaker (first-appearance) — STABLE colour basis, so a speaker
	// keeps its colour no matter which lanes are currently shown.
	const allSpeakers = $derived.by((): string[] => {
		const out: string[] = [];
		for (const t of turns) if (!out.includes(t.speaker)) out.push(t.speaker);
		return out;
	});
	// Lanes = only speakers with a turn in the VISIBLE window — keeps it compact
	// (Chunk scope shows the 1–3 people in this chunk; Video scope shows everyone).
	const speakers = $derived.by((): string[] => {
		const out: string[] = [];
		for (const t of turns)
			if (t.end > win.lo && t.start < win.hi && !out.includes(t.speaker)) out.push(t.speaker);
		return out;
	});

	// A CATEGORICAL data palette — one hue per speaker — not UI state, so the semantic-token rule
	// (bg-primary / text-muted-foreground) does not apply: no token set can express "N distinguishable
	// categories". These are the only raw palette values in the estate, and deliberately so.
	const PALETTE = [
		'bg-sky-500',
		'bg-emerald-500',
		'bg-amber-500',
		'bg-violet-500',
		'bg-rose-500',
		'bg-teal-500',
		'bg-orange-500',
		'bg-indigo-500',
	];
	const colorOf = (spk: string): string =>
		PALETTE[allSpeakers.indexOf(spk) % PALETTE.length] ?? PALETTE[0]!;

	const activeSpeaker = $derived(
		turns.find((t) => currentTime >= t.start && currentTime < t.end)?.speaker ?? null,
	);

	const pct = (t: number): number => Math.min(100, Math.max(0, ((t - win.lo) / span) * 100));
	const playFrac = $derived((currentTime - win.lo) / span);
	const playInWin = $derived(playFrac >= 0 && playFrac <= 1);

	const seekFromEvent = (e: MouseEvent, lane: HTMLElement) => {
		const rect = lane.getBoundingClientRect();
		if (rect.width <= 0) return;
		const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
		onSeek(win.lo + frac * span);
	};
</script>

{#if duration > 0 && turns.length > 0}
	<div class="flex flex-col gap-0.5 p-1.5" role="group" aria-label="Speaker timeline">
		<div class="text-muted-foreground mb-0.5 flex items-center gap-2 text-xs">
			<div class="border-border inline-flex overflow-hidden rounded-md border">
				<button
					type="button"
					onclick={() => (scope = 'chunk')}
					disabled={!canChunk}
					class="px-1.5 py-0.5 transition-colors disabled:opacity-40 {scope === 'chunk'
						? 'bg-secondary text-foreground'
						: 'hover:bg-muted hover:text-foreground'}"
				>
					Chunk
				</button>
				<button
					type="button"
					onclick={() => (scope = 'video')}
					class="border-border border-l px-1.5 py-0.5 transition-colors {scope === 'video'
						? 'bg-secondary text-foreground'
						: 'hover:bg-muted hover:text-foreground'}"
				>
					Video
				</button>
			</div>
			<span class="tabular-nums">
				{fmtTime(win.lo)} – {fmtTime(win.hi)} · {scope === 'chunk' ? 'current chunk' : 'full video'}
			</span>
		</div>

		{#each speakers as speaker (speaker)}
			{@const c = colorOf(speaker)}
			{@const isActiveLane = speaker === activeSpeaker}
			<div class="flex items-center gap-2">
				<!-- w-24 only when the voice button renders, so lanes keep their exact
             old width (and bar alignment) until the voice tables are built. -->
				<div
					class="flex {showVoice
						? 'w-24'
						: 'w-20'} shrink-0 items-center gap-1 text-[0.7rem] font-medium"
				>
					<span class="size-1.5 shrink-0 rounded-sm {c}"></span>
					<span class="truncate {isActiveLane ? 'text-foreground' : 'text-muted-foreground'}">
						{speaker}
					</span>
					{#if showVoice}
						<button
							type="button"
							title="Find this voice in other videos"
							aria-label={`Find ${speaker}'s voice in other videos`}
							onclick={() => onFindVoice?.({ speaker })}
							class="text-muted-foreground hover:bg-muted hover:text-foreground ml-auto shrink-0 rounded-md p-0.5 transition-colors"
						>
							<AudioLines class="size-3" />
						</button>
					{/if}
				</div>
				<div
					class="bg-secondary/40 relative h-3 w-full overflow-x-hidden rounded-sm {isActiveLane
						? 'ring-primary/50 ring-1'
						: ''}"
					role="presentation"
					onclick={(e) => seekFromEvent(e, e.currentTarget)}
				>
					{#each turns as t (t.turn_id)}
						{#if t.speaker === speaker && t.end > win.lo && t.start < win.hi}
							{@const left = pct(t.start)}
							{@const isCurrent = currentTime >= t.start && currentTime < t.end}
							<button
								type="button"
								title={`${speaker} · ${fmtTime(t.start)} → ${fmtTime(t.end)}` +
	(showVoice ? ' · right-click: find this voice' : '')}
								aria-label={`${speaker} from ${fmtTime(t.start)} to ${fmtTime(t.end)}`}
								onclick={(e) => {
	e.stopPropagation();
	onSeek(t.start);
}}
								oncontextmenu={(e) => {
	// Secondary action: anchor on THIS turn's voiceprint instead
	// of the speaker centroid. No-op (native menu) until built.
	if (!showVoice) return;
	e.preventDefault();
	e.stopPropagation();
	onFindVoice?.({ turnId: t.turn_id });
}}
								class="absolute top-0 h-full cursor-pointer rounded-sm transition-colors hover:brightness-110 {c} {isCurrent
									? 'ring-primary ring-1'
									: 'opacity-70'}"
								style="left: {left}%; width: {Math.max(0.4, pct(t.end) - left)}%;"
							></button>
						{/if}
					{/each}
					{#if playInWin}
						<div
							class="bg-primary pointer-events-none absolute top-0 z-10 h-full w-0.5"
							style="left: {playFrac * 100}%;"
						></div>
					{/if}
				</div>
			</div>
		{/each}
	</div>
{/if}
