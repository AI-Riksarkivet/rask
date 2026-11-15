<script lang="ts">
	// The tier badge: the tier a namespace DECLARES (`rask.tier`, ADR 0160), never one read off its name.
	// Presentation-only — the read is `namespaceTier` / `NamespaceTiers`.
	import type { StageInfo } from './stage';

	let { info }: { info: StageInfo } = $props();
</script>

<span class="stage mono {info.stage}" title="Tier declared on the namespace (rask.tier)">
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
