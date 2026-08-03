<script lang="ts">
	import { Database, HardDrive, Layers } from '@lucide/svelte';
	import { Badge } from '@rask/ui/badge';
	import { listStoresByTier } from '$lib/storage/remote/storage.remote';
	import type { Store } from '$lib/storage/storage';

	// The tier -> store view (R28). Grouping is done by the CATALOG (`GET /v1/stores/tiers`), not
	// here: a store's tier is a governed fact, and transcribing it into the page that displays it is
	// exactly the duplication this gate removed — the old hardcoded bucket union was two copies of
	// one fact in two languages, and a third copy in a template would be no better.
	let tiers = $state<Record<string, Store[]> | null>(null);
	let status = $state(0);
	let detail = $state('');
	let settled = $state(false);

	// The three GOVERNED tiers, in cascade order (R23). Rendered even when empty — "no store backs
	// silver here" is a fact worth showing, and a blank page cannot distinguish that from a failed
	// read. Roles outside the medallion follow, because raw is the external world, not a tier.
	const GOVERNED = ['bronze', 'silver', 'gold'] as const;

	const extraRoles = $derived(
		Object.keys(tiers ?? {})
			.filter((r) => !GOVERNED.includes(r as (typeof GOVERNED)[number]))
			.sort(),
	);

	async function load(): Promise<void> {
		settled = false;
		const res = await listStoresByTier();
		settled = true;
		if (res.ok) {
			tiers = res.data;
			status = 200;
			detail = '';
		} else {
			status = res.status;
			detail = res.detail;
		}
	}

	$effect(() => {
		void load();
	});
</script>

<svelte:head><title>Stores by tier · Catalog</title></svelte:head>

<header class="head">
	<h1><Layers size={18} /> Stores by tier</h1>
	<p>
		Every object store the catalog knows, grouped by the role it was registered with. The medallion is
		exactly bronze → silver → gold; raw is the external world and never a governed tier.
	</p>
</header>

{#if !settled}
	<p class="muted">Loading the registry…</p>
{:else if status !== 200}
	<p class="err">
		Catalog unreachable (HTTP {status}){detail ? ` — ${detail}` : ''}.
	</p>
{:else if tiers}
	<section class="tiers">
		{#each GOVERNED as role (role)}
			{@const stores = tiers[role] ?? []}
			<article class="tier" data-role={role}>
				<h2><Database size={14} /> {role}<Badge variant="secondary">governed</Badge></h2>
				{#if stores.length === 0}
					<p class="muted">No store backs {role} on this estate.</p>
				{:else}
					{@render storeList(stores)}
				{/if}
			</article>
		{/each}
		{#each extraRoles as role (role)}
			<article class="tier" data-role={role}>
				<h2><HardDrive size={14} /> {role}</h2>
				{@render storeList(tiers[role] ?? [])}
			</article>
		{/each}
	</section>
{/if}

{#snippet storeList(stores: Store[])}
	<ul>
		{#each stores as store (store.name)}
			<li>
				<span class="name">{store.name}</span>
				<span class="bucket">s3://{store.bucket}</span>
				{#if store.read_only}<Badge variant="outline">read-only</Badge>{/if}
				<p class="desc">{store.description}</p>
			</li>
		{/each}
	</ul>
{/snippet}

<style>
	.head {
		padding: 1.25rem 1.5rem 0.5rem;
	}
	h1 {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		font-size: 1.15rem;
		font-weight: 600;
	}
	.head p {
		color: var(--color-muted-foreground);
		font-size: 0.85rem;
		margin-top: 0.35rem;
		max-width: 62ch;
	}
	.tiers {
		display: grid;
		gap: 0.75rem;
		padding: 0.75rem 1.5rem 2rem;
		grid-template-columns: repeat(auto-fill, minmax(20rem, 1fr));
	}
	.tier {
		border: 1px solid var(--color-border);
		border-radius: var(--radius-lg);
		background: var(--color-card);
		padding: 0.85rem 1rem;
	}
	h2 {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		font-size: 0.9rem;
		font-weight: 600;
		text-transform: capitalize;
	}
	ul {
		margin-top: 0.6rem;
		display: grid;
		gap: 0.6rem;
	}
	li {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.4rem;
	}
	.name {
		font-weight: 500;
		font-size: 0.85rem;
	}
	.bucket {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		color: var(--color-muted-foreground);
	}
	.desc {
		flex-basis: 100%;
		font-size: 0.75rem;
		color: var(--color-muted-foreground);
	}
	.muted {
		color: var(--color-muted-foreground);
		font-size: 0.8rem;
		padding: 0 1.5rem;
	}
	.err {
		color: var(--color-destructive);
		font-size: 0.85rem;
		padding: 0 1.5rem;
	}
</style>
