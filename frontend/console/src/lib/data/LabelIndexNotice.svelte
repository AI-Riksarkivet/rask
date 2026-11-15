<script lang="ts">
	/**
	 * Which projects' labels a list could not read, and why: a row in such a project shows no label,
	 * and a label filter cannot find it there, so the list says so rather than passing it off as
	 * unlabelled. When the projects to ask could not be read at all, it is said while a label filter is
	 * on, as the project filter says that same failure only while it is on.
	 */
	import type { LabelReadFailure } from './label-index.svelte.ts';

	let { failures, filtering }: { failures: readonly LabelReadFailure[]; filtering: boolean } =
		$props();

	const shown = $derived(failures.filter((f) => f.project !== null || filtering));
</script>

{#if shown.length > 0}
	<div class="text-muted-foreground mb-2 text-xs" data-testid="label-read-notice">
		{#each shown as f (f.project)}
			<p>
				{#if f.project === null}
					No labels could be read ({f.reason}){filtering
						? ', so the filter cannot find what carries the label'
						: ''}.
				{:else}
					The labels of project <code class="font-mono">{f.project}</code> could not be read ({f.reason}){filtering
						? ', so an object there carrying the label may be missing'
						: ''}.
				{/if}
			</p>
		{/each}
	</div>
{/if}
