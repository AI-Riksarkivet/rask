<script lang="ts">
	// "Connect your compute": what a pipeline on its own engine runs to reach this table or namespace
	// through the catalog's doors, in pylance and in LanceDB. The bearer is read from the environment,
	// so the page holds and shows no secret, and the storage credential is the one the catalog vends.
	import { CopyButton } from '@riksarkivet/ui/copy-button';
	import { auth } from '#lib/auth.svelte.ts';
	import { CATALOG_API } from '#lib/upstreams.ts';
	import { connectSnippets } from './connect';

	let { id, kind }: { id: string; kind: 'table' | 'namespace' } = $props();

	const snippets = $derived(
		connectSnippets({
			catalogUrl: `${location.origin}${CATALOG_API}`,
			id,
			kind,
			signIn: auth.enabled && auth.issuer ? { issuer: auth.issuer, scopes: auth.scopes } : null,
		}),
	);
	const clients = $derived([
		{ name: 'pylance', install: 'pylance lance-namespace', code: snippets.pylance },
		{ name: 'LanceDB', install: 'lancedb', code: snippets.lancedb },
	]);
</script>

<section class="flex flex-col gap-4" data-testid="connect">
	<p class="text-muted-foreground text-sm">
		Any engine reaches this {kind} through the catalog's Lance REST namespace, signed in as you. Set
		<code class="font-mono">RASK_TOKEN</code> to your bearer and run one of these.
	</p>
	{#each clients as client (client.name)}
		<div class="flex flex-col gap-1.5" data-testid="connect-{client.name.toLowerCase()}">
			<div class="flex items-center gap-2">
				<h3 class="text-sm font-medium">{client.name}</h3>
				<span class="text-muted-foreground font-mono text-xs">pip install {client.install}</span>
				<CopyButton value={client.code} label="{client.name} snippet" class="ml-auto" />
			</div>
			<pre
        class="bg-muted overflow-x-auto rounded-md border p-3 font-mono text-xs leading-relaxed"><code
          >{client.code}</code
        ></pre>
		</div>
	{/each}
</section>
