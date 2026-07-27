<script lang="ts">
	// The dataset panel's origin + schema-time-travel section: WHO originated the table (#creator — the
	// verified catalog principal at create time, distinct from "who wrote the latest version") and WHAT its
	// columns looked like at each Lance version (#24 /schema?version=N). Both were API-only until now. The
	// creator line loads eagerly (one cheap read); the schema viewer is collapsible + lazy, with a version
	// selector fed by the dataset's producing-run versions so the user can step back through the history.
	// All state is keyed by the dataset it belongs to (mirrors GrantsPanel) — no reset effect, no cross-
	// dataset bleed: a stale fetch for a clicked-away dataset never lands (latest-wins by derivation).
	import { ChevronRight, Table2, UserRound } from '@lucide/svelte';
	import { fetchCreator, fetchSchema } from '$lib/api';
	import type { SchemaField } from '@rask/api/lineage';

	// `versions` = the distinct Lance versions this dataset was written at (newest-first), from its runs.
	let { dataset, versions }: { dataset: string; versions: string[] } = $props();

	let creatorState = $state<{ for: string; name: string | null } | null>(null);
	let openedFor = $state<string | null>(null);
	// `selected` undefined = latest. `fields` null = loading. `version` = the version the schema is FOR.
	let schema = $state<{
		for: string;
		selected: number | undefined;
		fields: SchemaField[] | null;
		version: number | null;
	} | null>(null);

	const creator = $derived(creatorState?.for === dataset ? creatorState.name : null);
	const open = $derived(openedFor === dataset);
	const shown = $derived(schema?.for === dataset ? schema : null);

	$effect(() => {
		const current = dataset;
		fetchCreator(current).then((c) => {
			if (dataset === current) creatorState = { for: current, name: c?.creator ?? null };
		});
	});

	async function load(version: number | undefined): Promise<void> {
		const current = dataset;
		schema = { for: current, selected: version, fields: null, version: null };
		const res = await fetchSchema(current, version);
		if (dataset !== current) return; // latest-wins
		schema = {
			for: current,
			selected: version,
			fields: res?.fields ?? [],
			version: res?.version ?? null,
		};
	}

	function toggle(): void {
		if (open) {
			openedFor = null;
			return;
		}
		openedFor = dataset;
		if (shown === null) load(undefined);
	}
</script>

<div class="prov">
	{#if creator}
		<p class="creator">
			<UserRound size={11} /> created by <span class="who mono">{creator}</span>
		</p>
	{/if}

	<button class="head" onclick={toggle} aria-expanded={open}>
		<span class="chev" class:open><ChevronRight size={12} /></span>
		<Table2 size={12} />
		<span
			>Schema{#if shown?.version != null}<span class="at"> @v{shown.version}</span>{/if}</span
		>
	</button>
	{#if open}
		{#if versions.length > 0}
			<div class="vers">
				<button class="ver" class:on={shown?.selected === undefined} onclick={() => load(undefined)}
					>latest</button
				>
				{#each versions as v (v)}
					<button
						class="ver mono"
						class:on={shown?.selected === Number(v)}
						onclick={() => load(Number(v))}>v{v}</button
					>
				{/each}
			</div>
		{/if}
		{#if !shown || shown.fields === null}
			<p class="mut">Loading schema…</p>
		{:else if shown.fields.length === 0}
			<p class="mut">No schema recorded for this version.</p>
		{:else}
			<table class="sch">
				<tbody>
					{#each shown.fields as f (f.name)}
						<tr>
							<td class="fname mono">{f.name}</td>
							<td class="ftype mono">{f.type}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	{/if}
</div>

<style>
	.prov {
		display: flex;
		flex-direction: column;
		gap: 6px;
		margin: 2px 0 10px;
	}
	.creator {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		color: var(--mut);
		font-size: 11px;
		margin: 0;
	}
	.creator .who {
		color: var(--ink);
	}
	.head {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		background: none;
		border: none;
		padding: 0;
		color: var(--mut);
		font: inherit;
		font-size: 12px;
		cursor: pointer;
	}
	.head:hover {
		color: var(--ink);
	}
	.at {
		color: var(--faint);
	}
	.chev {
		display: inline-flex;
		transition: transform 0.12s ease;
	}
	.chev.open {
		transform: rotate(90deg);
	}
	.vers {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
	}
	.ver {
		font-size: 10px;
		padding: 1px 8px;
		border: 1px solid var(--line);
		border-radius: 999px;
		background: none;
		color: var(--mut);
		cursor: pointer;
	}
	.ver.on {
		background: var(--panel-2);
		color: var(--ink);
		border-color: color-mix(in srgb, var(--accent, var(--ink)) 40%, var(--line));
	}
	.sch {
		border-collapse: collapse;
		font-size: 11px;
	}
	.sch td {
		padding: 1px 14px 1px 0;
		vertical-align: top;
	}
	.fname {
		color: var(--ink);
	}
	.ftype {
		color: var(--faint);
	}
	.mut {
		color: var(--faint);
		font-size: 11px;
		margin: 0;
	}
</style>
