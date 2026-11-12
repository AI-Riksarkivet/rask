<script lang="ts">
	/**
	 * What a filtered list shows instead of rows when its filter cannot narrow them: the filter names
	 * no project the caller can see (`reason` null), or the list cannot tell its rows' projects
	 * (`reason` says why). Either way it shows no rows, filtered or not, so nothing on screen claims to
	 * be narrowed when it is not, and it offers the way back to every project.
	 */
	import { Button } from '@rask/ui/button';

	let {
		project,
		reason = null,
		onclear,
	}: { project: string; reason?: string | null; onclear: () => void } = $props();
</script>

<div
	class="border-border bg-card flex flex-wrap items-center gap-3 rounded-md border p-3 text-sm"
	role="alert"
	data-testid="project-filter-notice"
>
	<p class="min-w-0 flex-1">
		{#if reason === null}
			No project <code class="font-mono">{project}</code> that you can see: it does not exist, or you
			hold no grant on it.
		{:else}
			The filter to project <code class="font-mono">{project}</code> cannot be applied: {reason}.
		{/if}
	</p>
	<Button size="sm" variant="outline" onclick={onclear}>Show all projects</Button>
</div>
