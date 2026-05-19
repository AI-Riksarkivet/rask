<script lang="ts">
	let { message }: { message: string } = $props();
</script>

<div class="rounded-lg border border-dashed p-8 text-center">
	<p class="text-muted-foreground">{message}</p>
</div>
