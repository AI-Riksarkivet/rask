<script lang="ts">
	import { Popover } from 'bits-ui';
	import { Settings2 } from '@lucide/svelte';
	import { voiceSearch } from '$lib/voice-search.svelte';
	import { buttonVariants } from '@rask/ui';
	import { Select } from '@rask/ui/select';
	import { Field } from '@rask/ui/field';
	import { RadioGroup, type RadioOption } from '@rask/ui/radio-group';
	import { type SelectOption } from '@rask/ui/select';
	import { Slider } from '@rask/ui/slider';
	import { Switch } from '@rask/ui/switch';

	/**
	 * The single "config" surface for a search. All values are bindable and owned
	 * by the parent <SearchBar>; they take effect on the next Search submit (the
	 * query box and these settings are applied together).
	 *
	 * `kind` is read-only here — it decides which sections are relevant (balance
	 * only matters for Hybrid; match style only when keyword is involved).
	 */
	let {
		kind,
		resultN = $bindable('100'),
		rerank = $bindable(false),
		rerankN = $bindable('20'),
		weightPct = $bindable<number | null>(null),
		style = $bindable('loose'),
		sceneMethod = $bindable('vector'),
	}: {
		kind: string;
		resultN?: string;
		rerank?: boolean;
		rerankN?: string;
		weightPct?: number | null;
		style?: string;
		sceneMethod?: string;
	} = $props();

	const resultOptions: SelectOption[] = [20, 50, 100, 200].map((n) => ({
		value: String(n),
		label: String(n),
	}));
	const rerankOptions: SelectOption[] = [10, 20, 50, 100].map((n) => ({
		value: String(n),
		label: String(n),
	}));
	const matchOptions: RadioOption[] = [
		{ value: 'loose', label: 'Loose', description: 'Words anywhere in the chunk; stem-aware.' },
		{ value: 'phrase', label: 'Phrase', description: 'Exact words, consecutive order.' },
		{ value: 'fuzzy', label: 'Fuzzy', description: 'Allow up to 2 typos per word.' },
	];
	// How the Scene kind searches the frame caption: by meaning (vector) or words (BM25).
	const sceneOptions: RadioOption[] = [
		{
			value: 'vector',
			label: 'Meaning',
			description: 'Vector search over the caption — semantically similar scenes.',
		},
		{
			value: 'fts',
			label: 'Keyword',
			description: 'BM25 over the caption text — exact Swedish words in the scene description.',
		},
	];

	// Balance is stored as weightPct ∈ [0,100] | null (null = parameter-free RRF). `weightPct` is the
	// SINGLE source of truth and is read, never mirrored.
	//
	// It used to be copied into `auto` + `weightVal` $state at creation and synced back with an $effect.
	// Both copies were initialised exactly once, so the moment the parent replaced `weightPct` from the
	// outside — `adoptSpec` (search-bar.svelte) loading a saved view, which persists `weight` — the copies
	// were stale AND the effect promptly wrote the stale value back over the adopted one. The Settings
	// panel then displayed a fusion balance the search was not running, and reopening the popover did not
	// help: <SearchSettings> sits outside the {#if}, so the locals survive. Found by an audit of runes
	// usage and confirmed link by link.
	//
	// `auto` and the slider position are now DERIVED from the prop; only `lastManual` is local, because
	// "where the slider was before you switched to Auto" is genuinely not expressible in `weightPct`
	// (null carries no number). User interaction writes the prop directly through the components'
	// change callbacks, so there is one direction of flow and nothing to keep in sync.
	let lastManual = $state(weightPct ?? 50);
	const auto = $derived(weightPct === null);
	const weightVal = $derived(weightPct ?? lastManual);

	function setAuto(on: boolean): void {
		weightPct = on ? null : lastManual;
	}

	function setWeight(value: number): void {
		lastManual = value;
		weightPct = value;
	}

	const balanceLabel = $derived(
		auto
			? 'Auto (RRF)'
			: weightVal === 50
				? 'balanced'
				: weightVal < 50
					? `${100 - weightVal}% keyword`
					: `${weightVal}% vector`,
	);
</script>

<Popover.Root>
	<Popover.Trigger
		class={buttonVariants({ variant: 'outline', size: 'sm' })}
		title="Search settings — result count, reranking, fusion balance, match style"
	>
		<Settings2 />
		<span>Settings</span>
	</Popover.Trigger>

	<Popover.Portal>
		<Popover.Content
			sideOffset={6}
			align="end"
			class="border-border bg-popover text-popover-foreground z-50 flex w-[320px] flex-col gap-3 rounded-lg border p-4 text-xs shadow-md"
		>
			<Field label="Results to return" inline>
				<Select bind:value={resultN} options={resultOptions} ariaLabel="Results to return" />
			</Field>

			<Field label="Rerank results" inline>
				<Switch bind:checked={rerank} aria-label="Rerank results" />
			</Field>
			{#if rerank}
				<Field
					label="Rerank top"
					description="Cross-encoder re-scores this many top results (the rest keep their order). Smaller = faster, more precise head."
					inline
				>
					<Select bind:value={rerankN} options={rerankOptions} ariaLabel="Rerank candidates" />
				</Field>
			{/if}

			{#if kind === 'both'}
				<div class="border-border flex flex-col gap-1.5 border-t pt-3">
					<div class="flex items-baseline justify-between">
						<span class="text-foreground text-xs font-medium">Fusion balance</span>
						<span class="text-muted-foreground text-xs">{balanceLabel}</span>
					</div>
					<label class="flex items-center justify-between">
						<span class="text-muted-foreground text-xs">Auto-fuse (RRF)</span>
						<Switch checked={auto} onCheckedChange={setAuto} aria-label="Auto-fuse with RRF" />
					</label>
					{#if !auto}
						<Slider
							value={weightVal}
							onValueChange={setWeight}
							min={0}
							max={100}
							step={5}
							aria-label="Fusion balance"
						/>
						<div class="text-muted-foreground flex justify-between text-[0.7rem]">
							<span>← keyword</span>
							<span>vector →</span>
						</div>
					{/if}
				</div>
			{/if}

			{#if kind === 'scene'}
				<Field
					label="Scene search"
					description="Search the AI caption of each frame by meaning (vector) or exact words (keyword)."
					class="border-border border-t pt-3"
				>
					<RadioGroup bind:value={sceneMethod} options={sceneOptions} />
				</Field>
			{/if}

			{#if kind !== 'meaning' && kind !== 'scene'}
				<Field label="Keyword match style" class="border-border border-t pt-3">
					<RadioGroup bind:value={style} options={matchOptions} />
				</Field>
			{/if}

			{#if voiceSearch.built}
				<!-- Voice search ("Find this voice") is query-by-example, not a text
             mode — its only knob lives here. Bound straight to the shared
             store; the search page re-runs an active voice query on change. -->
				<div class="border-border flex flex-col gap-1 border-t pt-3">
					<Field label="Voice: include same video" inline>
						<Switch
							bind:checked={voiceSearch.includeSameDoc}
							aria-label="Voice results: include the anchor's own video"
						/>
					</Field>
					<span class="text-muted-foreground text-xs">
						"Find this voice" normally hides matches from the anchor's own video. Applies
						immediately to an active voice search.
					</span>
				</div>
			{/if}
		</Popover.Content>
	</Popover.Portal>
</Popover.Root>
