<script lang="ts">
	// FEW-SHOT PROPAGATION — "label a few, apply to all", as a control instead of an architecture.
	//
	// The seam existed end to end (`controller.propagate` → the jobs plane, exemplar ids resolved,
	// scope levels typed, idempotent job ids) and NOTHING called it: the one flow the batch plane
	// was designed around had no button. This panel is that button. The picked annotations are the
	// EXEMPLARS; the scope says how far their pattern reaches — this item, or the whole dataset.
	//
	// Submission is fire-and-forget by design (results surface async as `prediction` rows the
	// review queue ranks); the OUTCOME line answers the only question the submitter has right now —
	// queued where, or refused why. With no jobs runner deployed the service answers with an
	// honest mock and the toast says so; this panel repeats it rather than reading as success.
	import { Send } from '@lucide/svelte';
	import { Button } from '@rask/ui/button';
	import { reviewSelection } from '$lib/labeling/review-selection.svelte';
	import type { AnnotatorController } from '../annotator.svelte';

	let { controller }: { controller: AnnotatorController } = $props();

	/** Every unit of the OPEN ITEM — the multi-key selection the canvas is walking. The scope's
	 *  useful middle: exemplars labelled on THIS page, pattern applied across all N pages of the
	 *  item (`keys` is plural on the wire for exactly this). */
	const itemKeys = $derived(reviewSelection.units.map((u) => u.key));

	/** The exemplar rows: the multi-selection when there is one, else the inspected row. */
	const exemplars = $derived(
		controller.selectedSet.size > 0
			? [...controller.selectedSet]
			: controller.selectedIndex !== null && controller.selectedIndex >= 0
				? [controller.selectedIndex]
				: [],
	);

	let scope = $state<'item' | 'selection' | 'corpus'>('item');
	let submitted = $state<string | null>(null);

	// The WIRE truth wins over the synchronous "a submit left": the fire-and-forget's answer
	// lands on `controller.lastJob`, and a transport refusal must not leave "queued" as the last
	// word on screen.
	const outcome = $derived.by<string | null>(() => {
		const job = controller.lastJob;
		if (submitted === null) return null;
		if (job === null) return submitted;
		if ('error' in job) return `submit failed — ${job.error}`;
		return job.backend === 'mock'
			? `queued as ${job.id} on the MOCK — no jobs runner deployed, nothing will run`
			: `queued as ${job.id}`;
	});

	function run(): void {
		const key = controller.unitKey;
		const target: import('@rask/labeling/types').Selection | null =
			scope === 'corpus'
				? { level: 'corpus' }
				: scope === 'selection' && itemKeys.length > 0
					? { level: 'chunks', keys: itemKeys }
					: key
						? { level: 'chunks', keys: [key] }
						: null;
		if (!target) {
			submitted = 'this canvas has no unit key — open an item first';
			return;
		}
		controller.lastJob = null;
		const result = controller.propagate(target, exemplars);
		submitted =
			result.status === 'queued'
				? 'submitting…'
				: result.status === 'unsupported'
					? (result.reason ?? 'unsupported')
					: result.status;
	}
</script>

{#if exemplars.length > 0}
	<div class="border-border flex flex-col gap-2 border-t p-3" data-testid="propagate-panel">
		<div class="flex items-center justify-between">
			<span class="text-muted-foreground text-xs font-medium">Propagate</span>
			<span class="text-muted-foreground text-[11px] tabular-nums" data-testid="propagate-count">
				{exemplars.length} exemplar{exemplars.length === 1 ? '' : 's'}
			</span>
		</div>
		<p class="text-muted-foreground text-[11px]">
			The selected annotation{exemplars.length === 1 ? '' : 's'} become the few-shot examples; a batch run
			applies the pattern across the scope and lands predictions in the review queue.
		</p>
		<div class="flex items-center gap-1.5">
			<select
				class="border-input bg-background h-7 flex-1 rounded-md border px-2 text-xs"
				bind:value={scope}
				data-testid="propagate-scope"
			>
				<option value="item">this item</option>
				{#if itemKeys.length > 1}
					<option value="selection">all {itemKeys.length} items here</option>
				{/if}
				<option value="corpus">entire dataset</option>
			</select>
			<Button size="sm" data-testid="propagate-run" onclick={run}>
				<Send class="size-3.5" /> Propagate
			</Button>
		</div>
		{#if outcome}
			<p class="text-muted-foreground text-[11px]" data-testid="propagate-outcome">{outcome}</p>
		{/if}
	</div>
{/if}
