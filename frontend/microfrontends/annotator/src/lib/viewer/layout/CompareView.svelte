<script lang="ts">
	// THE COMPARE VIEW — the multi-key item laid out as CANDIDATES, side by side.
	//
	// This is the pairwise/preference question (Label Studio's `Pairwise`, Argilla's preference
	// shapes): the person judges BETWEEN the items, so the workspace shows them together and puts
	// the ontology's choices under each. A pick is the same item-level `tag` row a
	// ClassificationBar toggle writes — on the WINNING unit's own annotations — so the judgment
	// lands in the ordinary review queue with a version guard, not in a parallel verdict store.
	//
	// Writes are per-pane version-guarded deltas over the plain transport (`loadAnnotations` /
	// `postSave`). Deliberately NOT a canvas controller per pane: the controller's autosave is a
	// debounce with no unmount flush, and two mounted controllers would be two writers racing the
	// same table the moment panes shared a key. The view REPLACES the canvas while open — the
	// route mounts one or the other, never both.
	import { ArrowLeft, Check } from '@lucide/svelte';
	import { Button } from '@rask/ui/button';
	import { toast } from 'svelte-sonner';

	import {
		AnnotationsHttpError,
		loadAnnotations,
		postSave,
	} from '@rask/labeling/annotations-client';

	import { fetchTask } from '$lib/projects/remote/tasks.remote';
	import type { MediaUnit } from '../types';
	import type { TextLaneLine } from '../text-lane';
	import { paneOrdinal, paneTags, paneTextLines, toggleTagPayload, type PaneTag } from '../compare';
	import { spanColor } from '../span-render';
	import MediaThumb from './MediaThumb.svelte';

	let {
		units,
		taskId,
		canvasHref,
	}: {
		/** Every unit of the open item — one pane each. */
		units: MediaUnit[];
		/** The task this item came from — source of the ontology's choices. Null = ad-hoc. */
		taskId: string | null;
		/** Back to the one-at-a-time canvas (the same URL without `view=compare`). */
		canvasHref: string;
	} = $props();

	/** The choices, from the task's ontology (`tag` classes — the same set the classification bar
	 *  offers). An ad-hoc item has no contract, so the panes render but offer no chips. */
	let choices = $state<string[]>([]);
	$effect(() => {
		const id = taskId;
		if (!id) {
			choices = [];
			return;
		}
		let alive = true;
		void fetchTask({ taskId: id })
			.then((result) => {
				if (!alive) return;
				choices = result.ok
					? (result.data.ontology?.classes ?? [])
							.filter((c) => c.tools.includes('tag'))
							.map((c) => c.name)
					: [];
			})
			.catch(() => {
				if (alive) choices = [];
			});
		return () => {
			alive = false;
		};
	});

	interface Pane {
		tags: PaneTag[];
		version: number;
		lines: TextLaneLine[];
		state: 'loading' | 'ready' | 'failed';
		busy: boolean;
	}
	// Keyed by unit key, not index: the panes reload independently and a stale in-flight read must
	// not land on whichever pane now sits at its old position.
	const panes = $state<Record<string, Pane>>({});

	async function loadPane(unit: MediaUnit): Promise<void> {
		panes[unit.key] = { tags: [], version: 0, lines: [], state: 'loading', busy: false };
		try {
			const { table, version } = await loadAnnotations(unit.annotationsUrl);
			panes[unit.key] = {
				tags: paneTags(table),
				version,
				lines: unit.kind === 'text' ? paneTextLines(table) : [],
				state: 'ready',
				busy: false,
			};
		} catch {
			panes[unit.key] = { tags: [], version: 0, lines: [], state: 'failed', busy: false };
		}
	}

	$effect(() => {
		for (const unit of units) if (!panes[unit.key]) void loadPane(unit);
	});

	/** One chip toggle = one guarded save + a reload of THAT pane. A 409 means someone else moved
	 *  the pane's table under the read — the reload shows their state and the person re-decides. */
	async function toggle(unit: MediaUnit, label: string): Promise<void> {
		const pane = panes[unit.key];
		if (!pane || pane.state !== 'ready' || pane.busy) return;
		pane.busy = true;
		try {
			const { status } = await postSave(
				unit.annotationsUrl,
				toggleTagPayload(pane.tags, label, pane.version),
			);
			if (status === 'conflict') toast.warning('This item changed under you — reloaded');
		} catch (e) {
			toast.error(
				e instanceof AnnotationsHttpError ? `save failed (HTTP ${e.status})` : 'save failed',
			);
		}
		await loadPane(unit);
	}

	const applied = (key: string, label: string): boolean =>
		panes[key]?.tags.some((t) => t.label === label) ?? false;
</script>

<div class="bg-background flex h-full min-h-0 w-full flex-col" data-testid="compare-view">
	<header class="border-border bg-card flex shrink-0 items-center gap-3 border-b px-3 py-2">
		<Button variant="ghost" size="sm" href={canvasHref} data-testid="exit-compare">
			<ArrowLeft class="size-4" />
			Canvas
		</Button>
		<h1 class="text-sm font-medium">Compare {units.length} candidates</h1>
		<span class="text-muted-foreground text-xs">
			{choices.length > 0
				? 'pick per candidate — the choice saves onto that item'
				: 'no choices — this item was opened without a task'}
		</span>
	</header>

	<div
		class="grid min-h-0 flex-1 gap-3 overflow-y-auto p-3"
		style={`grid-template-columns: repeat(${Math.min(units.length, 3)}, minmax(0, 1fr))`}
	>
		{#each units as unit, i (unit.key)}
			{@const pane = panes[unit.key]}
			<section
				class="border-border bg-card flex min-h-0 flex-col overflow-hidden rounded-lg border"
				data-testid={`compare-pane-${paneOrdinal(i)}`}
			>
				<div class="border-border flex items-center gap-2 border-b px-3 py-1.5">
					<span
						class="bg-primary text-primary-foreground flex size-5 items-center justify-center rounded text-xs font-semibold"
					>
						{paneOrdinal(i)}
					</span>
					<span class="text-muted-foreground truncate font-mono text-[11px]">{unit.key}</span>
					{#if pane?.state === 'failed'}
						<span class="text-destructive ml-auto text-[11px]">failed to load</span>
					{:else if (pane?.tags.length ?? 0) > 0}
						<span class="text-muted-foreground ml-auto text-[11px]" data-testid="pane-applied">
							{pane!.tags.length} applied
						</span>
					{/if}
				</div>

				<div class="min-h-0 flex-1 overflow-y-auto">
					{#if unit.kind === 'text'}
						<!-- The text IS the candidate — glyph tiles cannot be compared. Same reading
						     order as the transcription lane. -->
						<div class="space-y-1.5 p-3" data-testid="pane-text">
							{#each pane?.lines ?? [] as line (line.id)}
								<p class="text-sm leading-6">{line.text}</p>
							{:else}
								<p class="text-muted-foreground text-xs">
									{pane?.state === 'loading' ? 'loading…' : 'no text on this item'}
								</p>
							{/each}
						</div>
					{:else}
						<MediaThumb src={unit.imageUrl} kind={unit.kind} ratio="aspect-[4/3]" fit="contain" />
					{/if}
				</div>

				{#if choices.length > 0}
					<div class="border-border flex flex-wrap items-center gap-1.5 border-t px-3 py-2">
						{#each choices as c (c)}
							<Button
								variant={applied(unit.key, c) ? 'secondary' : 'outline'}
								size="xs"
								style={`border-bottom: 2px solid ${spanColor(c, choices)}`}
								aria-pressed={applied(unit.key, c)}
								disabled={pane?.state !== 'ready' || pane.busy}
								data-testid={`prefer-${paneOrdinal(i)}-${c}`}
								onclick={() => toggle(unit, c)}
							>
								{#if applied(unit.key, c)}<Check class="size-3" />{/if}
								{c}
							</Button>
						{/each}
					</div>
				{/if}
			</section>
		{/each}
	</div>
</div>
