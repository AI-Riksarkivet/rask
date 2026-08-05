<script lang="ts">
	// QUICK VIEW — review one item beside the queue, without leaving it.
	//
	// A pre-labelled queue is REVIEWED, not drawn on: look at the image, check the suggested class is
	// right, accept it or send it back. Doing that today means clicking "Annotate →", landing on the
	// canvas route, deciding, and navigating back — losing the filter, the page and the selection
	// each time. Fifty-seven items is fifty-seven round trips through a page you did not want to
	// leave.
	//
	// Deliberately NOT the drawing canvas. It shows the item and its verdict actions; an item that
	// needs shapes drawn still belongs on the canvas, and the link to it is right here. Pretending a
	// drawer is a PixiJS surface would be a worse lie than not offering one.
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	// NAMESPACE import, the estate's convention for this component (TenantsPanel, DlqPanel). The
	// package also exports `Root as Sheet`, so `import { Sheet }` compiles and then renders a bare
	// Root where `Sheet.Content` was meant — a dialog with no content and no error.
	import * as Sheet from '@rask/ui/sheet';
	import { ChevronLeft, ChevronRight, ExternalLink } from '@lucide/svelte';

	import { predictedLabels } from './item-columns';
	import { EVENT_LABELS, taskStateVariant } from './presentation.js';
	import { neighbours } from './quick-view';
	import { taskImageUrl } from './queue-view';
	import type { TaskDetail } from './types.js';

	let {
		open = $bindable(false),
		taskId = $bindable<string | null>(null),
		tasks,
		apiUrl,
		canvasHref,
		busy = false,
		onfire,
	}: {
		open?: boolean;
		/** Which task is open. Bindable so prev/next move it without the parent mediating. */
		taskId?: string | null;
		/** The rows currently in view — the TABLE's page, so prev/next walk exactly what the filter
		 *  and sort produced rather than whatever order the server returned. */
		tasks: TaskDetail[];
		apiUrl: (path: string) => string;
		canvasHref: (task: TaskDetail) => string;
		busy?: boolean;
		/** Fire one of the task's own legal events. The parent owns the call so the drawer and the
		 *  row buttons share one code path and one refusal surface. */
		onfire?: (task: TaskDetail, event: string) => void;
	} = $props();

	const nav = $derived(neighbours(tasks, taskId));
	const task = $derived(tasks.find((t) => t.task_id === taskId) ?? null);

	/** The task's OWN legal events, minus the two that need typed input — a recipient, a note. Those
	 *  stay on the row where their dialogs live; a drawer that opened a second dialog on top of
	 *  itself would be a worse answer than sending you back to the row. */
	const events = $derived(
		(task?.legal_events ?? [])
			.map((e) => e.event)
			.filter((e) => e !== 'assign' && e !== 'request_changes' && e !== 'save_draft'),
	);
</script>

<Sheet.Root bind:open>
	<Sheet.Content side="right" class="flex w-full flex-col gap-3 sm:max-w-lg">
		{#if task}
			<Sheet.Header>
				<Sheet.Title class="flex items-center gap-2">
					<span class="truncate font-mono text-sm">{task.source.keys.join(', ')}</span>
					<Badge variant={taskStateVariant(task.state)}>{task.state.replace('_', ' ')}</Badge>
				</Sheet.Title>
				<Sheet.Description>
					{nav.position} of {nav.total} in view
					{#if task.source.where}· <span class="font-mono">{task.source.where}</span>{/if}
				</Sheet.Description>
			</Sheet.Header>

			{#key task.task_id}
				{@const src = taskImageUrl(task, apiUrl)}
				{#if src}
					<img
						{src}
						alt=""
						class="bg-muted max-h-[45vh] w-full rounded-md object-contain"
						onerror={(e) => ((e.currentTarget as HTMLImageElement).style.visibility = 'hidden')}
					/>
				{:else}
					<div
						class="bg-muted text-muted-foreground flex h-40 items-center justify-center rounded-md text-xs"
					>
						no preview
					</div>
				{/if}
			{/key}

			<!-- The SUGGESTED label. `secondary`, never the default variant: the entire job of this
			     drawer is deciding whether the suggestion is right, so it must not already look
			     accepted. -->
			{#if predictedLabels(task).length > 0}
				<div class="flex flex-wrap items-center gap-1 text-xs">
					<span class="text-muted-foreground">suggested:</span>
					{#each predictedLabels(task) as label (label)}
						<Badge variant="secondary">{label}</Badge>
					{/each}
				</div>
			{/if}

			{#if task.review_notes.length > 0}
				<ul class="text-muted-foreground flex flex-col gap-1 text-xs">
					{#each task.review_notes as note, i (i)}
						<li><span class="font-medium">{note.by}:</span> {note.message}</li>
					{/each}
				</ul>
			{/if}

			<!-- The item's own legal events, from the task machine — the same source the row buttons
			     read, so the drawer can never offer an action the row would refuse. -->
			<div class="flex flex-wrap gap-1">
				{#each events as event (event)}
					<Button
						size="sm"
						variant={event === 'accept' ? 'default' : 'outline'}
						disabled={busy}
						data-testid="quick-{event}"
						onclick={() => onfire?.(task, event)}
					>
						{EVENT_LABELS[event] ?? event}
					</Button>
				{/each}
				<Button size="sm" variant="ghost" href={canvasHref(task)}>
					<ExternalLink class="size-3.5" /> Canvas
				</Button>
			</div>

			<div class="mt-auto flex items-center justify-between gap-2 border-t pt-3">
				<Button
					size="sm"
					variant="outline"
					disabled={!nav.prev}
					data-testid="quick-prev"
					onclick={() => (taskId = nav.prev?.task_id ?? taskId)}
				>
					<ChevronLeft class="size-4" /> Prev
				</Button>
				<Button
					size="sm"
					variant="outline"
					disabled={!nav.next}
					data-testid="quick-next"
					onclick={() => (taskId = nav.next?.task_id ?? taskId)}
				>
					Next <ChevronRight class="size-4" />
				</Button>
			</div>
		{/if}
	</Sheet.Content>
</Sheet.Root>
