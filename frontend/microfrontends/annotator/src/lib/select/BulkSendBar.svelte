<script lang="ts">
	// Send a browse SELECTION into a labeling task, in bulk.
	//
	// Step 1 of `open_browse.md`. Browse could always find keys and could only ever open them one at a
	// time on the canvas; sending them into a project was a per-project dialog you had to already be
	// standing in. So the workflow this product exists for — "find five hundred pages like this one,
	// label them" — had no path through it.
	//
	// Nothing new server-side: this is the existing `sendItems` command, given a selection and a
	// project chooser. Bulk labeling is deliberately NOT a new write path.
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { Select } from '@rask/ui/select';

	import { listProjects, sendItems } from '$lib/projects/remote/projects.remote';
	import { itemsFromSelection, refuseReason } from './bulk-send';

	let {
		keys,
		dataset = null,
		tenant = 'default',
		onsent,
	}: {
		/** The keys currently selected in the browser. */
		keys: string[];
		/** The corpus they came from — rides on every item so they resolve against it, not the default. */
		dataset?: string | null;
		tenant?: string;
		/** Fired after a successful send, with how many items landed. */
		onsent?: (sent: number) => void;
	} = $props();

	// EMPTY STRING is "nothing chosen", because `Select` declares `value = $bindable('')` — a
	// bindable with a fallback cannot be bound to `undefined`, and doing so throws
	// `props_invalid_value` at RENDER time. svelte-check passed (its .d.ts says `string | undefined`);
	// only opening the page caught it, which is why the bar silently never appeared.
	let projectId = $state('');
	let sending = $state(false);
	let error = $state('');
	let sent = $state<number | null>(null);

	/** Loaded on demand — the bar only appears once something is selected, so this never runs on a
	 *  browse page nobody is selecting on. */
	const projects = $derived(keys.length > 0 ? listProjects({ tenant }) : null);

	/** Tasks that can still RECEIVE items. A published project refuses the send server-side (§5.2:
	 *  provenance is frozen with the artifact), so offering it would be offering a guaranteed 409. */
	const options = $derived(
		(projects?.current?.ok ? projects.current.data.projects : [])
			.filter((p) => p.state === 'draft' || p.state === 'labeling')
			.map((p) => ({ value: p.project_id, label: `${p.title ?? p.slug} · ${p.state}` })),
	);

	/** The server's rules, asked BEFORE the click: an empty selection, no project, or over the cap. */
	const refusal = $derived(refuseReason(keys, projectId || null));

	async function send(): Promise<void> {
		if (sending || refusal !== null || !projectId) return;
		sending = true;
		error = '';
		sent = null;
		const result = await sendItems({
			projectId,
			items: itemsFromSelection(keys, dataset),
		});
		sending = false;
		if (result.ok) {
			sent = result.data.sent;
			onsent?.(result.data.sent);
			return;
		}
		// The server's own words. A send can be refused for reasons this bar cannot know — a frozen
		// project, a dataset that no longer resolves, a missing rung — and paraphrasing them into
		// "send failed" throws away the only thing that lets someone act.
		error = result.detail;
	}
</script>

<!-- Rendered while there is a selection OR a result to report. Gating on the selection alone made
     the "N sent" confirmation unreachable: a successful send clears the selection, which unmounted
     the bar that was about to show it. Caught by driving it, not by reading it. -->
{#if keys.length > 0 || sent !== null}
	<div
		class="border-border bg-card/40 flex flex-wrap items-center gap-2 border-t px-4 py-2"
		data-testid="bulk-send-bar"
	>
		<Badge variant="secondary" data-testid="bulk-selected">
			{keys.length.toLocaleString()} selected
		</Badge>

		<Select bind:value={projectId} ariaLabel="Labeling task" placeholder="Send into…" {options} />

		<Button
			size="sm"
			disabled={sending || refusal !== null}
			title={refusal ?? 'send the selected items into this labeling task'}
			data-testid="bulk-send"
			onclick={() => void send()}
		>
			{sending ? 'Sending…' : 'Send'}
		</Button>

		<!-- The refusal is SHOWN, not only used to disable the button. A disabled control with no
		     reason beside it is the shape people file bugs about. -->
		{#if refusal && keys.length > 0}
			<span class="text-muted-foreground text-xs" data-testid="bulk-refusal">{refusal}</span>
		{/if}
		{#if error}
			<span class="text-destructive text-xs" data-testid="bulk-error">{error}</span>
		{/if}
		<!-- Only once the selection is empty, i.e. straight after a send. A new selection means the
		     previous count is stale, and a stale confirmation beside fresh work reads as this work
		     having already been sent. -->
		{#if sent !== null && keys.length === 0}
			<span class="text-muted-foreground text-xs" data-testid="bulk-sent">
				{sent.toLocaleString()} sent
			</span>
		{/if}
	</div>
{/if}
