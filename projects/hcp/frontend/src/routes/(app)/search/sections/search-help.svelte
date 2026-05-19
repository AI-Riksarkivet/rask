<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import Info from 'lucide-svelte/icons/info';

	let {
		mode,
	}: {
		mode: 'objects' | 'operations';
	} = $props();
</script>

<Card.Root>
	<Card.Content class="py-4">
		<div
			class="mb-3 flex gap-2 rounded-md border border-blue-200 bg-blue-50 p-3 dark:border-blue-900 dark:bg-blue-950/50"
		>
			<Info class="mt-0.5 h-4 w-4 shrink-0 text-blue-600 dark:text-blue-400" />
			<div class="text-xs text-blue-800 dark:text-blue-300">
				<p class="mb-1 font-medium">Prerequisites</p>
				<ul class="list-inside list-disc space-y-0.5">
					<li>
						<strong>Search enabled on namespace</strong> — Each namespace must have
						<code class="rounded bg-blue-100 px-1 dark:bg-blue-900/50">searchEnabled</code> set to
						<code class="rounded bg-blue-100 px-1 dark:bg-blue-900/50">true</code> in its configuration
					</li>
					<li>
						<strong>User permissions</strong> — The user account must have
						<code class="rounded bg-blue-100 px-1 dark:bg-blue-900/50">SEARCH</code> permission on the
						target namespaces
					</li>
					<li>
						<strong>Indexing active</strong> — HCP's indexing engine must have indexed the objects (new
						objects may take time to appear)
					</li>
				</ul>
			</div>
		</div>
		{#if mode === 'objects'}
			<div class="space-y-3 text-sm">
				<p class="text-foreground">
					<strong>Object Search</strong> queries the HCP Metadata Query Engine. It searches indexed metadata
					for all objects stored across your namespaces. Results include file paths, sizes, content types,
					owners, and timestamps.
				</p>
				<div class="grid gap-4 sm:grid-cols-2">
					<div>
						<p class="mb-1.5 font-medium text-foreground">Query syntax</p>
						<p class="mb-2 text-muted-foreground">
							Queries use <code class="rounded bg-muted px-1">field:value</code> format. Use
							<code class="rounded bg-muted px-1">*:*</code> to match everything. Combine with
							<code class="rounded bg-muted px-1">AND</code>,
							<code class="rounded bg-muted px-1">OR</code>,
							<code class="rounded bg-muted px-1">NOT</code> operators.
						</p>
						<ul class="space-y-1 text-muted-foreground">
							<li>
								<code class="rounded bg-muted px-1">namespace:documents</code> — filter by namespace
							</li>
							<li>
								<code class="rounded bg-muted px-1">contentType:application/pdf</code> — by MIME type
							</li>
							<li>
								<code class="rounded bg-muted px-1">owner:jdoe</code> — by object owner
							</li>
							<li>
								<code class="rounded bg-muted px-1">objectPath:reports/*</code> — path wildcard
							</li>
						</ul>
					</div>
					<div>
						<p class="mb-1.5 font-medium text-foreground">Range &amp; boolean queries</p>
						<ul class="space-y-1 text-muted-foreground">
							<li>
								<code class="rounded bg-muted px-1">size:[1024 TO *]</code> — objects larger than 1 KB
							</li>
							<li>
								<code class="rounded bg-muted px-1">size:[* TO 1048576]</code> — smaller than 1 MB
							</li>
							<li>
								<code class="rounded bg-muted px-1">hold:true</code> — objects under legal hold
							</li>
							<li>
								<code class="rounded bg-muted px-1">customMetadata:true AND namespace:logs</code>
								— combine conditions
							</li>
						</ul>
					</div>
				</div>
				<p class="text-xs text-muted-foreground">
					Click column headers to sort results server-side. Use the filter inputs below headers to
					narrow results client-side within the current page.
				</p>
			</div>
		{:else}
			<div class="space-y-3 text-sm">
				<p class="text-foreground">
					<strong>Operation Search</strong> queries the HCP audit log for object lifecycle events. It
					shows when objects were created, deleted, or purged across your namespaces. This is useful for
					auditing changes and tracking data modifications.
				</p>
				<p class="text-muted-foreground">
					Use the operation type checkboxes to filter by event type. Optionally filter to a specific
					namespace. Results are sorted with the most recent operations first.
				</p>
			</div>
		{/if}
	</Card.Content>
</Card.Root>
