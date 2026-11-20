<script lang="ts">
	// A catalog object's name on its own page: the path a person reads as the heading (`acme › bronze ›
	// orders`), and beneath it the `$` identifier the doors take, as something to copy into a client or a
	// call. A warehouse's id is one segment, the same as its path, so it has no second line.
	import { CopyButton } from '@riksarkivet/ui/copy-button';
	import { DELIMITER } from '#lib/api/identifiers.ts';
	import { catalogPath } from '#lib/catalog-trail.ts';

	let { id, kind }: { id: string; kind: 'table' | 'namespace' } = $props();
</script>

<span class="flex min-w-0 flex-col gap-0.5" data-slot="catalog-name">
	<h1 class="text-lg font-semibold break-words">{catalogPath(id)}</h1>
	{#if id.includes(DELIMITER)}
		<span class="text-muted-foreground inline-flex items-center gap-1 font-mono text-xs">
			<span class="select-all break-all" data-testid="catalog-id">{id}</span>
			<CopyButton value={id} label="{kind} id" />
		</span>
	{/if}
</span>
