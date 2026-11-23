<script lang="ts">
	// The inspector's answer: the verdict of a "why" query, its export as a committable `.fga.yaml`
	// test, and the derivation (or the result of a "what can" / "who can" query) hop by hop.
	import { Button } from '@riksarkivet/ui/button';
	import { Check, CircleCheck, CircleX, ClipboardCopy } from '@lucide/svelte';
	import { assertionName, toFgaYaml } from './assertion';
	import type { Tuple } from '../access';

	type Props = {
		verdict: { allowed: boolean; user: string; relation: string; object: string } | null;
		/** The derivation, already flattened to words by the explorer — rendered here as the chain. */
		chain: readonly string[];
		/** The stored tuples the current verdict rests on — exported WITH the assertion so the pasted
		 *  test is self-contained. See `toFgaYaml`: `fga model test` never reads the live store. */
		supportingTuples: readonly Tuple[];
	};

	let { verdict, chain, supportingTuples }: Props = $props();

	/**
	 * The verdict, rendered as a committable `.fga.yaml` test.
	 *
	 * Distinguishing a check RUN (a fact about this cluster now) from one COMMITTED (a fact CI enforces)
	 * is the whole point — they are not the same claim, and only the second survives the next deploy.
	 */
	const assertionYaml = $derived.by(() => {
		if (!verdict) return '';
		const a = {
			user: verdict.user,
			object: verdict.object,
			relation: verdict.relation,
			allowed: verdict.allowed,
		};
		return toFgaYaml(assertionName(a), [a], supportingTuples);
	});
	let copied = $state(false);

	async function copyAssertion(): Promise<void> {
		if (!assertionYaml) return;
		try {
			await navigator.clipboard.writeText(assertionYaml);
			copied = true;
			setTimeout(() => (copied = false), 2000);
		} catch (err) {
			// A denied clipboard permission is normal, not exceptional — the <pre> below is always
			// present and selectable, so the export never depends on the API being available.
			console.error(`clipboard write failed: ${String(err)}`);
		}
	}
</script>

{#if verdict}
	<section
		class="flex flex-col gap-2 rounded-md border p-3 {verdict.allowed
			? 'border-success/50 bg-success/10'
			: 'border-destructive/50 bg-destructive/10'}"
	>
		<div class="flex items-center gap-2">
			{#if verdict.allowed}
				<CircleCheck size={20} class="text-success" />
				<span class="text-lg font-bold tracking-wide text-success">ALLOWED</span>
			{:else}
				<CircleX size={20} class="text-destructive" />
				<span class="text-lg font-bold tracking-wide text-destructive">DENIED</span>
			{/if}
		</div>
		<p class="font-mono text-[11px] text-muted-foreground">
			({verdict.user}, {verdict.relation}, {verdict.object})
		</p>
		{#if chain.length === 0}
			<p class="text-[11px] text-muted-foreground">
				{verdict.allowed
					? 'Allowed, but the store returned no derivation — the grant is direct.'
					: 'Denied, and nothing in the model derives it.'}
			</p>
		{/if}
	</section>

	<section class="flex flex-col gap-1.5">
		<div class="flex items-center justify-between gap-2">
			<h3 class="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
				Commit as a test
			</h3>
			<Button size="sm" variant="outline" class="h-6 text-[11px]" onclick={copyAssertion}>
				{#if copied}<Check size={11} /> Copied{:else}<ClipboardCopy size={11} /> Copy{/if}
			</Button>
		</div>
		<p class="text-[11px] text-muted-foreground">
			This verdict is true <em>now</em>. Paste into
			<span class="font-mono">model.fga.yaml</span> and CI enforces it on every change.
		</p>
		<pre
			class="overflow-x-auto rounded border border-border bg-muted p-2 font-mono text-[10px] leading-relaxed">{assertionYaml}</pre>
	</section>
{/if}

<!-- The result summary is OUTSIDE the verdict block on purpose. Only "why" produces a verdict;
     "what can" and "who can" produce an answer with no boolean attached, and rendering their
     summary inside `{#if verdict}` would display nothing at all for those two queries. -->
{#if chain.length}
	<section class="flex flex-col gap-1">
		<h3 class="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
			{verdict ? 'Derivation' : 'Result'}
		</h3>
		<!-- Hop by hop. A permission that resolves through three objects looks identical to a direct
		     grant in a tuple table, and they are not the same thing to revoke. -->
		<ol class="flex flex-col gap-0.5">
			{#each chain as step, i (step)}
				<li class="flex gap-1.5 font-mono text-[11px]">
					{#if verdict}<span class="tabular-nums text-muted-foreground">{i + 1}.</span>{/if}
					<span class={step.includes('EXCLUDED') ? 'text-destructive' : ''}>{step}</span>
				</li>
			{/each}
		</ol>
	</section>
{/if}
