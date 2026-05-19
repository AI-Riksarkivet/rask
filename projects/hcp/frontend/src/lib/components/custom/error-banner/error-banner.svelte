<script lang="ts">
	let { message }: { message: string } = $props();
</script>

{#if message}
	<div
		class="rounded-lg border border-destructive/20 bg-destructive/10 p-3 text-sm text-destructive"
	>
		{message}
	</div>
{/if}
