<script lang="ts">
	// The medallion-tier badge (goal cond 3): a small stage chip derived from the namespace name.
	// Presentation-only — derivation lives in stage.ts.
	import type { StageInfo } from './stage';

	let { info }: { info: StageInfo } = $props();
</script>

<span class="stage mono {info.stage}" title="Medallion stage (derived from the namespace name)">
	{info.stage}
</span>

<style>
	.stage {
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		font-size: 11px;
		padding: 0 6px;
		color: var(--mut);
		white-space: nowrap;
	}
	.stage.bronze {
		color: var(--stage-bronze);
		border-color: color-mix(in srgb, var(--stage-bronze) 55%, var(--border));
	}
	.stage.silver {
		color: var(--stage-silver);
		border-color: color-mix(in srgb, var(--stage-silver) 55%, var(--border));
	}
	.stage.gold {
		color: var(--stage-gold);
		border-color: color-mix(in srgb, var(--stage-gold) 55%, var(--border));
	}
	.mono {
		font-family: ui-monospace, monospace;
	}
</style>
