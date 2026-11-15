<script lang="ts">
	/**
	 * An object's labels as chips, `name` or `name=value`, by name. Presentation only: the reads are
	 * `fetchObjectLabels` (one object) and `LabelIndexRead` (a list's rows).
	 */
	import { Badge } from '@rask/ui/badge';
	import { labelChips, type Labels } from './labels.ts';

	let { labels }: { labels: Labels } = $props();

	const chips = $derived(labelChips(labels));
</script>

{#if chips.length === 0}
	<!-- A monospace em dash, in the face of the list's other empty cells. -->
	<span class="text-muted-foreground font-mono">—</span>
{:else}
	<span class="inline-flex flex-wrap gap-1" data-slot="label-chips">
		{#each chips as chip (chip)}
			<Badge variant="outline" class="font-mono font-normal">{chip}</Badge>
		{/each}
	</span>
{/if}
