<script lang="ts">
	// The grant dialog's "Effect" box: ASSERT BEFORE YOU GRANT. Nothing is written: the store evaluates
	// the tuple as a hypothesis and answers with the delta. Offered rather than forced, because a
	// required round trip before every write turns a two-field dialog into a wizard. The dialog keys
	// this box by the grant it describes, so an answer never outlives the inputs it was asked about.
	import { Button } from '@riksarkivet/ui/button';
	import { simulate } from '../remote/access.api';

	type Props = {
		user: string;
		relation: string;
		object: string | null;
		/** Whether the grant carries what its condition needs; the store is not asked otherwise. */
		ready: boolean;
	};

	let { user, relation, object, ready }: Props = $props();

	/**
	 * What the proposed grant would actually change, answered by the store BEFORE anything is written.
	 *
	 * `allowed` on its own is unusable here: a grant that unlocks access and a grant that changes
	 * nothing both come back true. Only the delta against `baseline` separates them, and "this grant is
	 * a no-op" turns out to be the more common finding — a concentric model means the subject often
	 * already holds the access by inheritance, and granting again just adds a tuple somebody must later
	 * reason about when revoking.
	 */
	let preview = $state<{ allowed: boolean; baseline: boolean } | null>(null);
	let previewing = $state(false);
	let previewFailed = $state(false);

	async function runPreview(): Promise<void> {
		const subject = user.trim();
		const rel = relation.trim();
		if (!subject || !rel || !object || !ready) return;
		previewing = true;
		preview = null;
		previewFailed = false;
		try {
			// The question is deliberately about the RELATION BEING GRANTED, on the object in hand —
			// "would writing this tuple make this true" — rather than some derived `can_*` the dialog
			// would have to guess at.
			const res = await simulate({
				user: subject,
				relation: rel,
				object,
				hypothetical: [{ user: subject, relation: rel, object }],
			});
			if (!res.ok) {
				previewFailed = true;
				return;
			}
			preview = { allowed: res.data.allowed, baseline: res.data.baseline };
		} catch (err) {
			// A rejected remote call is a transport failure, not a verdict — the dialog says so.
			console.error(`simulate transport failure: ${String(err)}`);
			previewFailed = true;
		} finally {
			previewing = false;
		}
	}
</script>

<div class="flex flex-col gap-2 rounded-md border border-border p-2">
	<div class="flex items-center justify-between gap-2">
		<span class="text-xs font-medium">Effect</span>
		<Button
			size="sm"
			variant="outline"
			disabled={previewing || !ready || !user.trim() || !relation.trim()}
			onclick={runPreview}
		>
			{previewing ? 'Checking…' : 'Simulate'}
		</Button>
	</div>
	{#if previewFailed}
		<p class="text-[11px] text-muted-foreground">
			Could not simulate — the store did not answer. Granting anyway is still possible; its effect
			is simply unknown.
		</p>
	{:else if preview === null}
		<p class="text-[11px] text-muted-foreground">
			Ask the store what this would change, without writing it.
		</p>
	{:else if preview.baseline}
		<!-- The finding people least expect: in a concentric model the subject often already
		     holds the access by inheritance, so the grant adds a tuple that changes nothing and
		     that someone must later reason about when revoking. -->
		<p class="text-[11px] text-warning">
			<strong>No change.</strong> This subject already holds
			<span class="font-mono">{relation}</span> here — inherited, not stored. The tuple would be redundant.
		</p>
	{:else if preview.allowed}
		<p class="text-[11px] text-success">
			<strong>Grants access.</strong> Currently denied; after this write
			<span class="font-mono">{relation}</span> resolves.
		</p>
	{:else}
		<p class="text-[11px] text-destructive">
			<strong>Would not take effect.</strong> Even with this tuple the relation still does not resolve
			— the model excludes it, or something else is required.
		</p>
	{/if}
</div>
