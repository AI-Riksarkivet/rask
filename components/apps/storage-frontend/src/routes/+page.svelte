<script lang="ts">
	import { Badge } from '@rask/ui/badge';
	import { listObjects } from '$lib/remote/storage.remote';
	import { BUCKETS, type Bucket } from '$lib/storage';
	import { Database, Folder, FileText, ChevronRight, House, TriangleAlert } from 'lucide-svelte';

	let bucket = $state<Bucket>(BUCKETS[0]);
	let prefix = $state('');

	const segments = $derived(prefix.split('/').filter(Boolean));

	function openPrefix(p: string) {
		prefix = p;
	}
	function crumbTo(idx: number) {
		prefix = segments.slice(0, idx + 1).join('/') + '/';
	}
	function fmtSize(bytes: number): string {
		if (bytes < 1024) return `${bytes} B`;
		if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
		return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
	}
	function leaf(p: string): string {
		return p.replace(/\/$/, '').split('/').pop() ?? p;
	}
</script>

<div class="mx-auto flex w-full max-w-4xl flex-col gap-4 p-6">
	<div class="flex items-center gap-2">
		<Database class="text-muted-foreground h-5 w-5" />
		{#each BUCKETS as b (b)}
			<button
				class="rounded-md border px-3 py-1 text-sm {b === bucket
					? 'bg-primary text-primary-foreground'
					: 'hover:bg-accent'}"
				onclick={() => {
					bucket = b;
					prefix = '';
				}}
			>
				{b}
			</button>
		{/each}
	</div>

	<div class="text-muted-foreground flex flex-wrap items-center gap-1 text-sm">
		<button class="hover:text-foreground flex items-center gap-1" onclick={() => openPrefix('')}>
			<House class="h-3.5 w-3.5" />
			{bucket}
		</button>
		{#each segments as seg, i (i)}
			<ChevronRight class="h-3.5 w-3.5" />
			<button class="hover:text-foreground" onclick={() => crumbTo(i)}>{seg}</button>
		{/each}
	</div>

	<div class="bg-card overflow-hidden rounded-lg border">
		{#await listObjects({ bucket, prefix })}
			<div class="text-muted-foreground p-8 text-center text-sm">Loading {bucket}…</div>
		{:then listing}
			{#if listing.prefixes.length === 0 && listing.objects.length === 0}
				<div class="text-muted-foreground p-8 text-center text-sm">Empty prefix.</div>
			{:else}
				<table class="w-full text-sm">
					<thead class="text-muted-foreground border-b text-xs">
						<tr>
							<th class="px-4 py-2 text-left font-medium">Name</th>
							<th class="px-4 py-2 text-right font-medium">Size</th>
						</tr>
					</thead>
					<tbody>
						{#each listing.prefixes as p (p)}
							<tr class="hover:bg-accent/40 border-b last:border-0">
								<td class="px-4 py-2">
									<button class="flex items-center gap-2 font-medium" onclick={() => openPrefix(p)}>
										<Folder class="h-4 w-4 text-amber-500" />
										{leaf(p)}/
									</button>
								</td>
								<td class="text-muted-foreground px-4 py-2 text-right">—</td>
							</tr>
						{/each}
						{#each listing.objects as obj (obj.key)}
							<tr class="hover:bg-accent/40 border-b last:border-0">
								<td class="px-4 py-2">
									<span class="flex items-center gap-2">
										<FileText class="text-muted-foreground h-4 w-4" />
										{leaf(obj.key)}
									</span>
								</td>
								<td class="text-muted-foreground px-4 py-2 text-right font-mono text-xs">
									{fmtSize(obj.size)}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/if}
		{:catch}
			<div class="flex flex-col items-center gap-2 p-8 text-center">
				<TriangleAlert class="h-8 w-8 text-amber-500" />
				<p class="text-sm font-medium">Object listing endpoint pending</p>
				<p class="text-muted-foreground max-w-md text-xs">
					Calls the <code class="font-mono">listObjects</code> remote function (valibot, server-side, via
					the gateway). Needs <code class="font-mono">GET /api/volumes/objects</code> in volumes-api.
				</p>
				<!-- @rask/ui Badge — proves the shared component library works across a separate MFE app -->
				<Badge variant="warning">{bucket} · shared via @rask/ui</Badge>
			</div>
		{/await}
	</div>
</div>
