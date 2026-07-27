<script lang="ts">
	import { ShieldX } from '@lucide/svelte';

	// Shared role-blocked state: any zone route gated on an FGA privilege (estate admin, project
	// admin, …) renders THIS when the caller lacks it, so denial looks identical everywhere (the
	// app-error principle, but for authz: a 403 is an answer, not an error). The route decides the
	// wording; the default names no privilege to avoid leaking what gates what.
	let {
		title = 'Access denied',
		message = 'You do not have access to this page.',
		home = '/',
	}: { title?: string; message?: string; home?: string } = $props();
</script>

<div class="flex flex-1 items-center justify-center p-6">
	<div class="flex max-w-md flex-col items-center gap-3 text-center">
		<div
			class="bg-destructive/10 text-destructive flex size-12 items-center justify-center rounded-xl"
		>
			<ShieldX class="size-6" />
		</div>
		<div class="text-xl font-semibold">{title}</div>
		<p class="text-muted-foreground text-sm">{message}</p>
		<a href={home} data-sveltekit-reload class="text-primary text-sm hover:underline"
			>← Back to home</a
		>
	</div>
</div>
