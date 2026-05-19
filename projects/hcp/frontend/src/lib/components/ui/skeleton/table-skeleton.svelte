<script lang="ts">
	import { Skeleton } from '$lib/components/ui/skeleton/index.js';

	let { rows = 5, columns = 4 }: { rows?: number; columns?: number } = $props();
</script>

<div class="overflow-x-auto rounded-lg border">
	<table class="w-full text-left text-sm">
		<thead class="border-b bg-muted/50 text-xs uppercase tracking-wide text-muted-foreground">
			<tr>
				{#each Array(columns) as _, i}
					<th class="px-4 py-3">
						<Skeleton class="h-3 w-20" />
					</th>
				{/each}
			</tr>
		</thead>
		<tbody class="divide-y">
			{#each Array(rows) as _, r}
				<tr class="bg-card">
					{#each Array(columns) as _, c}
						<td class="px-4 py-3">
							<Skeleton class="h-4 {c === 0 ? 'w-32' : 'w-24'}" />
						</td>
					{/each}
				</tr>
			{/each}
		</tbody>
	</table>
</div>
