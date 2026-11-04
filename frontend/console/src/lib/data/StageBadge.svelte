<script lang="ts">
	// The medallion-tier badge (goal cond 3): a small stage chip derived from the namespace name.
	// Presentation-only — derivation lives in stage.ts.
	import type { StageInfo } from './stage';

	let { info }: { info: StageInfo } = $props();
</script>

<span
	class="stage mono {info.stage}"
	title="Medallion stage (derived from the namespace name{info.media
		? '; -media sibling zone'
		: ''})"
>
	{info.stage}{info.media ? '·media' : ''}
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
	.stage.raw {
		border-color: color-mix(in srgb, var(--faint) 55%, var(--line));
	}
	.stage.bronze {
		color: #b0763c;
		border-color: color-mix(in srgb, #b0763c 55%, var(--line));
	}
	.stage.silver {
		color: #9aa4b2;
		border-color: color-mix(in srgb, #9aa4b2 55%, var(--line));
	}
	.stage.gold {
		color: var(--accent, #ffc14d);
		border-color: color-mix(in srgb, var(--accent, #ffc14d) 55%, var(--line));
	}
	.mono {
		font-family: ui-monospace, monospace;
	}
</style>
