<script lang="ts">
	// A small labelled chip — match reasons, tags, statuses. Same API @rask/ui's Chip exposed
	// (label + tone), design-token styled so it themes with the rest of @rask/ui.
	let { label, tone = 'neutral' }: { label: string; tone?: 'neutral' | 'accent' } = $props();
</script>

<span
	data-slot="chip"
	class="inline-block rounded-full border px-1.5 py-0.5 text-[0.7rem] leading-tight whitespace-nowrap {tone === 'accent'
		? 'border-primary/30 bg-primary/10 text-primary font-semibold'
		: 'border-border bg-muted text-muted-foreground'}"
>
	{label}
</span>
