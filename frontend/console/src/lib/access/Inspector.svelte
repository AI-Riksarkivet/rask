<script lang="ts">
	// The right-hand drawer: the verdict, the selected node's raw tuples, and the model — the three
	// things that were separate tabs. They live here BESIDE the canvas, not instead of it, so reading a
	// tuple never costs you the graph you were reading it about.
	import { Badge } from '@riksarkivet/ui/badge';
	import { Button, buttonVariants } from '@riksarkivet/ui/button';
	import * as Collapsible from '@riksarkivet/ui/collapsible';
	import { Skeleton } from '@riksarkivet/ui/skeleton';
	import { cn } from '@riksarkivet/ui/utils';
	import { Plus, ShieldAlert, Trash2 } from '@lucide/svelte';
	import { Subject } from './identity/index.ts';
	import { timeBoxLabel } from './time-box.ts';
	import type { Tuple } from '../access';
	import { fetchObjectTuples } from '../remote/access.api';
	import GrantDialog from './InspectorGrantDialog.svelte';
	import RevokeDialog from './InspectorRevokeDialog.svelte';
	import Verdict from './InspectorVerdict.svelte';

	type Props = {
		selected: string | null;
		/** The name the selected table or namespace holds now; `selected` is its uuid object, as OpenFGA holds it. */
		selectedName: string | null;
		verdict: { allowed: boolean; user: string; relation: string; object: string } | null;
		/** The derivation, already flattened to words by the explorer — rendered here as the chain. */
		chain: readonly string[];
		dsl: string | null;
		/** Relations the MODEL says are directly assignable on the selected object's type. A grant can
		 *  only ever name one of these — a derived `can_*` is rejected by the catalog — so the dialog
		 *  offers exactly them instead of a free-text box that learns the rule from a 400. */
		assignableRelations: readonly string[];
		/** The signed-in principal key, so "grant to me" is one click and nobody has to type their own
		 *  `<idp-id>~<user id>`. */
		mySub: string | null;
		/** The stored tuples the current verdict rests on — exported WITH the assertion so the pasted
		 *  test is self-contained. See `toFgaYaml`: `fga model test` never reads the live store. */
		supportingTuples: readonly Tuple[];
		/** `{condition: {parameter: type}}` from the compiled model. Drives the grant dialog's TYPED
		 *  parameter inputs — a hand-kept copy would drift the moment the model gains a parameter, and a
		 *  missing CEL operand is a 503, not a soft failure. */
		modelConditions: Record<string, Record<string, string>>;
		onchanged: () => void;
	};

	let {
		selected,
		selectedName,
		verdict,
		chain,
		dsl,
		assignableRelations,
		mySub,
		supportingTuples,
		modelConditions,
		onchanged,
	}: Props = $props();

	// The tuples stored on the selected node: one read per object, held here and read below.
	const tuplesRead = $derived(selected ? fetchObjectTuples(selected) : null);
	const tuplesResult = $derived(tuplesRead?.current);
	const tuples = $derived(tuplesResult?.ok ? tuplesResult.data.tuples : null);
	const truncated = $derived(tuplesResult?.ok ? tuplesResult.data.truncated : false);
	/** 0 while the first answer is out, else the answer's status. */
	const status = $derived(
		tuplesResult === undefined ? 0 : tuplesResult.ok ? 200 : tuplesResult.status,
	);

	let message = $state<{ ok: boolean; text: string } | null>(null);
	let grantOpen = $state(false);
	let showModel = $state(false);
	let revokeTarget = $state<Tuple | null>(null);

	/** A write's outcome: say it, and on success read the node's tuples again and tell the explorer. */
	async function written(outcome: { ok: boolean; text: string }): Promise<void> {
		message = outcome;
		if (!outcome.ok) return;
		await tuplesRead?.refresh();
		onchanged();
	}
</script>

<aside class="flex w-96 shrink-0 flex-col gap-4 overflow-y-auto border-l border-border pl-4">
	<Verdict {verdict} {chain} {supportingTuples} />

	<section class="flex flex-col gap-2">
		<div class="flex items-center justify-between gap-2">
			<h2 class="text-xs font-semibold uppercase tracking-wider">Tuples</h2>
			{#if selected}
				<Button size="sm" variant="outline" onclick={() => (grantOpen = true)}>
					<Plus size={12} /> Grant
				</Button>
			{/if}
		</div>

		{#if !selected}
			<p class="text-xs text-muted-foreground">
				Select a node on the canvas to see the tuples stored on it.
			</p>
		{:else}
			{#if selectedName}
				<p class="truncate font-mono text-xs" title={selectedName}>{selectedName}</p>
			{/if}
			<p class="truncate font-mono text-[11px] text-muted-foreground" title={selected}>
				{selected}
			</p>
			{#if status === 0 && tuples === null}
				<Skeleton class="h-16 w-full" />
			{:else if status === 401 || status === 403}
				<p class="flex items-center gap-1.5 text-xs text-muted-foreground">
					<ShieldAlert size={13} /> The tuple store is estate-admin only.
				</p>
			{:else if tuples === null}
				<p class="text-xs text-muted-foreground">Tuple store unreachable (HTTP {status}).</p>
			{:else if tuples.length === 0}
				<p class="text-xs text-muted-foreground">
					No tuples stored on this object — any access it has is derived.
				</p>
			{:else}
				<ul class="flex flex-col gap-1">
					{#each tuples as t (`${t.user}|${t.relation}|${t.object}`)}
						{@const grantWindow = timeBoxLabel(t.condition, modelConditions)}
						<li class="flex items-center gap-2 rounded border border-border px-2 py-1">
							<div class="min-w-0 flex-1">
								<div class="text-[11px]"><Subject value={t.user} /></div>
								<Badge variant="secondary" class="mt-0.5 font-mono text-[9px]">{t.relation}</Badge>
								{#if grantWindow !== null}
									<p class="mt-0.5 text-[11px] text-muted-foreground">Time-boxed {grantWindow}</p>
								{/if}
							</div>
							<Button
								size="sm"
								variant="ghost"
								aria-label="Revoke {t.user} {t.relation} {t.object}"
								onclick={() => (revokeTarget = t)}
							>
								<Trash2 size={12} />
							</Button>
						</li>
					{/each}
				</ul>
				{#if truncated}
					<p class="mt-1 text-xs text-muted-foreground">
						Showing the first pages only: this object holds more tuples than the inspector reads.
					</p>
				{/if}
			{/if}
		{/if}

		{#if message}
			<p class="text-xs {message.ok ? 'text-success' : 'text-destructive'}">{message.text}</p>
		{/if}
	</section>

	{#if dsl}
		<Collapsible.Root bind:open={showModel} class="flex flex-col gap-2">
			<!-- "Model DSL", not "Model": the shared sidebar already owns a nav group called "Models", and
			     a bare "Model" collides with it for anything selecting by accessible name. -->
			<Collapsible.Trigger
				class={cn(
					buttonVariants({ variant: 'ghost', size: 'sm' }),
					'text-muted-foreground justify-between',
				)}
			>
				Model DSL {showModel ? '−' : '+'}
			</Collapsible.Trigger>
			<Collapsible.Content>
				<!-- Read-only, permanently. The model lives in three checked-in files with a CI drift gate;
				     a UI that mutated it would make that gate a liar. Tuples are data; the model is code. -->
				<pre
					class="max-h-72 overflow-auto rounded border border-border bg-muted p-2 font-mono text-[10px] leading-relaxed">{dsl}</pre>
			</Collapsible.Content>
		</Collapsible.Root>
	{/if}
</aside>

<GrantDialog
	bind:open={grantOpen}
	{selected}
	{selectedName}
	{assignableRelations}
	{mySub}
	{modelConditions}
	ondone={written}
/>

<RevokeDialog target={revokeTarget} onclose={() => (revokeTarget = null)} ondone={written} />
