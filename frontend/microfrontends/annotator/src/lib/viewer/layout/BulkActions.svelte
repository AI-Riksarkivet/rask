<script lang="ts">
	// Bulk-actions bar — shown when >1 annotation is multi-selected (Shift/Ctrl-click).
	// Accept/reject/label the whole selection at once (Label Studio "apply to selection"),
	// all through the controller's picked LabelOp seam.
	import { Check, X } from '@lucide/svelte';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import type { AnnotatorController } from '../annotator.svelte';

	let { controller }: { controller: AnnotatorController } = $props();
</script>

<div class="border-border bg-muted/40 flex flex-col gap-2 border-b p-3" data-testid="bulk-actions">
	<div class="flex items-center justify-between gap-2">
		<Badge variant="secondary" class="tabular-nums">{controller.selectedSet.size} selected</Badge>
		<Button variant="ghost" size="xs" onclick={() => controller.clearSelection()}>Clear</Button>
	</div>
	<div class="flex gap-1">
		<Button
			variant="outline"
			size="sm"
			class="flex-1"
			onclick={() => controller.bulkStatus('accepted')}
		>
			<Check class="size-3.5" /> Accept all
		</Button>
		<Button
			variant="outline"
			size="sm"
			class="flex-1"
			onclick={() => controller.bulkStatus('rejected')}
		>
			<X class="size-3.5" /> Reject all
		</Button>
	</div>
	{#if controller.labelClasses.length}
		<!-- Same quick-label buttons as the detail pane, so the two ways to relabel look alike. -->
		<div class="flex flex-wrap gap-1" title="Apply label to all selected">
			{#each controller.labelClasses as lc (lc)}
				<Button variant="outline" size="xs" onclick={() => controller.applyLabel(lc)}>
					{lc}
				</Button>
			{/each}
		</div>
	{/if}
</div>
