<script lang="ts">
	import RayShell from '$lib/components/layout/ray-shell.svelte';
	import { Card } from '$lib/components/ui/card';
	import { Badge } from '@your-repo/oxen/badge';
	import { Button } from '$lib/components/ui/button';
	import { listObjects } from '$lib/remote/storage.remote';
	import { BUCKETS, type Bucket } from '$lib/storage';
	import { Database, Folder, FileText, ChevronRight, House, TriangleAlert } from 'lucide-svelte';

	let bucket = $state<Bucket>(BUCKETS[0]);
	let prefix = $state('');

	/** Breadcrumb segments for the current prefix (trailing-slash delimited). */
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
	/** Last path segment of a key/prefix, for display. */
	function leaf(p: string): string {
		return p.replace(/\/$/, '').split('/').pop() ?? p;
	}
</script>

<RayShell title="Storage">
	<div class="mx-auto flex w-full max-w-4xl flex-col gap-4 p-6">
		<!-- Bucket selector -->
		<div class="flex items-center gap-2">
			<Database class="text-muted-foreground h-5 w-5" />
			{#each BUCKETS as b (b)}
				<Button
					variant={b === bucket ? 'default' : 'outline'}
					size="sm"
					onclick={() => {
						bucket = b;
						prefix = '';
					}}
				>
					{b}
				</Button>
			{/each}
		</div>

		<!-- Prefix breadcrumb -->
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

		<Card class="overflow-hidden">
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
								<th class="px-4 py-2 text-right font-medium">Modified</th>
							</tr>
						</thead>
						<tbody>
							{#each listing.prefixes as p (p)}
								<tr class="hover:bg-accent/40 border-b last:border-0">
									<td class="px-4 py-2">
										<button
											class="flex items-center gap-2 font-medium"
											onclick={() => openPrefix(p)}
										>
											<Folder class="h-4 w-4 text-amber-500" />
											{leaf(p)}/
										</button>
									</td>
									<td class="text-muted-foreground px-4 py-2 text-right">—</td>
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
									<td class="text-muted-foreground px-4 py-2 text-right font-mono text-xs">
										{obj.last_modified?.slice(0, 10) ?? '—'}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				{/if}
			{:catch}
				<!-- Backend endpoint not yet implemented — this is the documented "dummy"
				     scaffold showing the remote-function pattern. -->
				<div class="flex flex-col items-center gap-2 p-8 text-center">
					<TriangleAlert class="h-8 w-8 text-amber-500" />
					<p class="text-sm font-medium">Object listing endpoint pending</p>
					<p class="text-muted-foreground max-w-md text-xs">
						This view calls the <code class="font-mono">listObjects</code> remote function
						(valibot-validated, server-side, via the gateway). It needs
						<code class="font-mono">GET /api/volumes/objects?bucket=&prefix=</code> in volumes-api —
						a thin delimiter listing over <code class="font-mono">storage.s3_client</code>. See
						<code class="font-mono">docs/architecture/frontend-microfrontends.md</code>.
					</p>
					<Badge variant="outline">{bucket}</Badge>
				</div>
			{/await}
		</Card>
	</div>
</RayShell>
