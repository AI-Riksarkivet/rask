<script lang="ts">
	import { TriangleAlert } from '@lucide/svelte';

	// Shared error state for every app's +error.svelte, so a failed route in any
	// page degrades to the SAME branded page (resilient-page principle) instead of a blank
	// document or a per-app one-off. The app's +error.svelte passes page.status/page.error.
	let {
		status = 500,
		heading = 'Something went wrong',
		message = 'Something went wrong.',
		home = '/',
	}: {
		status?: number;
		/** The page's one h1, in words: what happened, not its code. */
		heading?: string;
		message?: string;
		home?: string;
	} = $props();
</script>

<div class="flex flex-1 items-center justify-center p-6">
	<div class="flex max-w-md flex-col items-center gap-3 text-center">
		<div
			class="bg-destructive/10 text-destructive flex size-12 items-center justify-center rounded-xl"
		>
			<TriangleAlert class="size-6" />
		</div>
		<h1 class="text-2xl font-semibold">{heading}</h1>
		<p class="text-muted-foreground text-sm">{message}</p>
		<p class="text-muted-foreground font-mono text-xs tabular-nums">HTTP {status}</p>
		<a href={home} class="text-primary text-sm hover:underline">← Back to home</a>
	</div>
</div>
