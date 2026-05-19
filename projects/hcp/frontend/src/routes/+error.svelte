<script lang="ts">
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import { Button } from '$lib/components/ui/button/index.js';

	// Auto-redirect to login on 401
	$effect(() => {
		if (page.status === 401) {
			goto('/login', { replaceState: true });
		}
	});
</script>

{#if page.status !== 401}
	<div class="flex min-h-screen items-center justify-center bg-background">
		<div class="text-center">
			<h1 class="text-6xl font-bold text-muted-foreground/50">
				{page.status}
			</h1>
			<p class="mt-4 text-lg text-muted-foreground">
				{page.error?.message ?? 'Something went wrong'}
			</p>
			<div class="mt-8">
				<Button onclick={() => (window.location.href = '/')}>Go Home</Button>
			</div>
		</div>
	</div>
{/if}
