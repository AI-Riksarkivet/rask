<script lang="ts">
	// The access-check simulator (#68): probe an arbitrary (user, relation) against one table or
	// namespace, the two kinds with a check door. Gated at the catalog on `can_review_access`, as the
	// review is. The verdict is keyed by the object it was asked about, so it never bleeds across a
	// navigation. The draft subject and relation are the panel's, bound in: this form lives inside the
	// review's collapsible and is unmounted when it closes, and the panel keeps them per object.
	import { Button } from '@riksarkivet/ui/button';
	import { Input } from '@riksarkivet/ui/input';
	import { Select } from '@riksarkivet/ui/select';
	import type { AccessKind } from '#lib/data/namespace.ts';
	import type { GrantsClient } from './grants-panel.svelte';
	import Subject from '../identity/subject.svelte';

	let {
		kind,
		dataset,
		relations,
		check,
		user = $bindable(''),
		relation = $bindable(''),
	}: {
		kind: AccessKind;
		dataset: string;
		/** Every can_* action the model defines on this type, held or not: what may be simulated. */
		relations: readonly string[];
		check: NonNullable<GrantsClient['checkAccess']>;
		/** The draft subject and relation, held by the panel. */
		user?: string;
		relation?: string;
	} = $props();
	let verdict = $state<{
		for: string;
		user: string;
		relation: string;
		allowed: boolean;
	} | null>(null);
	let busy = $state(false);
	let error = $state<string | null>(null);

	const shown = $derived(verdict?.for === dataset ? verdict : null);

	async function run(): Promise<void> {
		const who = user.trim();
		if (busy || !who || !relation) return;
		busy = true;
		error = null;
		const current = dataset;
		try {
			const res = await check(kind, current, who, relation);
			if (dataset !== current) return; // navigated away — drop the stale verdict
			if (res.ok) {
				verdict = {
					for: current,
					user: res.data.user,
					relation: res.data.relation,
					allowed: res.data.allowed,
				};
			} else if (res.status === 401 || res.status === 403) {
				error = `Simulating access needs the modifier rung on this ${kind}.`;
			} else {
				error = `Check failed (HTTP ${res.status}).`;
			}
		} finally {
			busy = false;
		}
	}
</script>

<div class="border-border/55 mt-2.5 border-t pt-2">
	<div class="text-muted-foreground mb-1.5 text-[11px] tracking-wider uppercase">
		Check a specific access
	</div>
	<div class="flex flex-wrap items-center gap-1.5">
		<Input
			class="min-w-40 flex-[1_1_220px] font-mono text-xs"
			placeholder="principal (idp~user id), or role:…#assignee"
			bind:value={user}
			onkeydown={(e) => e.key === 'Enter' && run()}
		/>
		<Select
			bind:value={relation}
			ariaLabel="Simulate action"
			placeholder="action…"
			options={relations.map((r) => ({ value: r, label: r }))}
		/>
		<Button variant="outline" size="sm" disabled={busy || !user.trim() || !relation} onclick={run}>
			{busy ? '…' : 'Check'}
		</Button>
	</div>
	{#if error}
		<p class="text-muted-foreground m-0 text-xs">{error}</p>
	{:else if shown}
		<p class={['mt-2 mb-0 text-xs', shown.allowed ? 'text-success' : 'text-destructive']}>
			<Subject value={shown.user} />
			{shown.allowed ? 'can' : 'cannot'}
			<span class="font-mono">{shown.relation}</span> on this {kind}.
		</p>
	{/if}
</div>
