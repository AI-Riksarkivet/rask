<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import { Skeleton } from '$lib/components/ui/skeleton/index.js';
</script>

<Card.Root>
	<Card.Content class="pt-6">
		<div class="flex items-center justify-between">
			<div class="space-y-2">
				<Skeleton class="h-3 w-24" />
				<Skeleton class="h-7 w-16" />
			</div>
			<Skeleton class="h-12 w-12 rounded-lg" />
		</div>
	</Card.Content>
</Card.Root>
