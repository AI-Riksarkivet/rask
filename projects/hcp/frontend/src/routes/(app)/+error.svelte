<script lang="ts">
	import { page } from '$app/state';
	import { Button } from '$lib/components/ui/button/index.js';
</script>

<div class="flex flex-1 items-center justify-center">
	<div class="text-center">
		<h1 class="text-6xl font-bold text-muted-foreground/50">
			{page.status}
		</h1>
		<p class="mt-4 text-lg text-muted-foreground">
			{page.error?.message ?? 'Something went wrong'}
		</p>
		<div class="mt-8 flex items-center justify-center gap-3">
			<Button href="/namespaces">Go to Namespaces</Button>
			<Button variant="outline" onclick={() => history.back()}>Go Back</Button>
		</div>
	</div>
</div>
