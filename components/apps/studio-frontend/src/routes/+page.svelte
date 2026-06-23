<script lang="ts">
	import { Shapes } from '@lucide/svelte';
</script>

<svelte:head><title>Studio — RASK</title></svelte:head>

<div class="flex flex-1 items-center justify-center p-6">
	<div class="flex max-w-md flex-col items-center gap-3 text-center">
		<div
			class="bg-sidebar-primary text-sidebar-primary-foreground flex size-12 items-center justify-center rounded-xl"
		>
			<Shapes class="size-6" />
		</div>
		<h1 class="text-xl font-semibold tracking-tight">Studio</h1>
		<p class="text-muted-foreground text-sm">
			Mini-applications built on this project's data and models. Coming soon.
		</p>
	</div>
</div>
