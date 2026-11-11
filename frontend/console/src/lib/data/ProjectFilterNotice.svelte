<script lang="ts">
	/**
	 * What a filtered list shows when it cannot tell its rows' projects: that the filter cannot be
	 * applied, why, and a way to clear it. It shows no rows, filtered or not, so nothing on screen
	 * claims to be narrowed when it is not.
	 */
	import { Button } from '@rask/ui/button';

	let { project, reason, onclear }: { project: string; reason: string; onclear: () => void } =
		$props();
</script>

<div
	class="border-border bg-card flex flex-wrap items-center gap-3 rounded-md border p-3 text-sm"
	role="alert"
>
	<p class="min-w-0 flex-1">
		The filter to project <code class="font-mono">{project}</code> cannot be applied: {reason}.
	</p>
	<Button size="sm" variant="outline" onclick={onclear}>Clear the filter</Button>
</div>
