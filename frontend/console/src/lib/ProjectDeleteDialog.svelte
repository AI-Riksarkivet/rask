<script lang="ts">
	import { heldChildren, refusedAsProtected, type ApiFailure } from '#lib/api/client.ts';
	import { resolve } from '$app/paths';
	// Retiring a tenant — the confirm side of `DELETE /management/v1/projects/{id}`.
	//
	// The whole reason this is a dialog and not a one-shot button is the 409: a project that still holds
	// warehouses refuses, and the refusal NAMES them in its `holds` member (its `detail` is rendered
	// verbatim, for the person). Those names are the user's next action, so they are rendered as LINKS straight to each warehouse in the lakehouse — the delete that unblocks
	// this one is then one click away instead of a manual hunt through the catalog. There is deliberately
	// no `cascade` on this route (a project cascade would reach a warehouse's bucket purge), so "go do the
	// rung below" is not a limitation to apologise for — it is the design, and the copy says so.
	//
	// Every refusal is rendered from the RFC 9457 `detail` VERBATIM. Those messages are written upstream to
	// be actionable and to name the fix; paraphrasing them here is how a UI ends up less informative than
	// curl. The glosses below ADD the thing a status code means and a sentence cannot (401 vs 404 vs a
	// timeout that may still have committed) — they never replace the message.
	import { ExternalLink, TriangleAlert } from '@lucide/svelte';
	import { AlertDialog } from '@riksarkivet/ui/alert-dialog';
	import { Button } from '@riksarkivet/ui/button';
	import { toast } from 'svelte-sonner';
	import { deleteProject } from '#lib/remote/warehouses.api.ts';

	let {
		open = $bindable(false),
		project,
		ondeleted,
	}: {
		open?: boolean;
		/** The tenant being retired — also the id the confirm posts. */
		project: string;
		/** Fired after a CONFIRMED delete (the response landed). The route navigates away on it: the
		 *  project no longer exists, so staying on its page would 404 on the next read. */
		ondeleted?: () => void;
	} = $props();

	let busy = $state(false);
	let force = $state(false);
	/** The last refusal, whole: the branches below read what the door's problem says in its own members
	 *  (a 409 means two different things, told apart by the problem's type), never the sentence. */
	let failure = $state.raw<ApiFailure | null>(null);

	/** The warehouses the refusal names as still held (`holds`): what to delete first. */
	const blockers = $derived(failure ? heldChildren(failure) : []);
	/** The OTHER 409: the record carries the `protected` flag. `force` is offered only here — it turns
	 *  exactly that lock and nothing else, so surfacing it on any other failure would misdescribe it. */
	const protectedRefusal = $derived(failure !== null && refusedAsProtected(failure));

	/** What the status MEANS, on top of the verbatim detail — never instead of it. */
	const gloss = $derived.by((): string | null => {
		if (!failure) return null;
		if (failure.status === 401) return 'Sign in — retiring a project is a per-user action.';
		if (failure.status === 404) {
			// The catalog collapses "no such project" and "not yours" on purpose: without it, any
			// authenticated caller could enumerate the estate's tenants by the 404-vs-403 difference. The
			// UI must not invent the distinction the API refuses to make.
			return `The catalog does not acknowledge ${project} for you — it either no longer exists or you do not administer it, and this door deliberately will not say which.`;
		}
		if (failure.status === 0) {
			// A DELETE that timed out may still have committed server-side; a refused connection cannot
			// have. Only the second may honestly claim "nothing was removed".
			return failure.timedOut
				? 'Timed out waiting on the catalog — the delete may or may not have been applied; re-read the projects list before retrying.'
				: 'Catalog unreachable — nothing was deleted.';
		}
		return null;
	});

	// Reset on CLOSE, not on open.
	//
	// Both are the same requirement — the next dialog must start clean, because a refusal describes an
	// estate the user has very likely just gone and CHANGED (that is the entire point of the blocker
	// links) — but only one of them is observable here. Bits UI calls `onOpenChange` from its own setter
	// (`bits-ui/dist/bits/dialog/components/dialog.svelte`), so it fires for the closes Bits performs
	// (Cancel, Escape) and NOT for a parent writing the bound `open`. This dialog has no `Trigger`: the
	// page's danger-zone button is what opens it, by assignment. Resetting on `next === true` therefore
	// never runs at all, and the stale state it was meant to clear survives — with `force` still armed,
	// and, because the confirm is disabled while blockers stand, with nothing on the reopened screen able
	// to re-enable it. Doing it on the close Bits DOES report leaves the same guarantee with no `$effect`
	// and no previous-value bookkeeping. (The success path closes by assignment and skips this; it
	// navigates away, so there is no next dialog to poison.)
	function onOpenChange(next: boolean): void {
		if (next) return;
		failure = null;
		force = false;
	}

	async function confirm(): Promise<void> {
		if (busy) return;
		busy = true;
		failure = null;
		try {
			// `force` is sent only when opted in — an explicit `false` would read on the wire as a
			// considered choice to leave protection on, which is not what an untouched checkbox means.
			const res = await deleteProject(force ? { project, force: true } : { project });
			if (!res.ok) {
				failure = res;
				return;
			}
			// The toast is written from the RESPONSE, not the request: `tuples_revoked` is the one fact only
			// the catalog holds, and `0` on an FGA-off stack is a real answer worth showing.
			const n = res.data.tuples_revoked;
			toast.success(
				`Project ${res.data.project} retired — ${n} tuple${n === 1 ? '' : 's'} revoked.`,
			);
			open = false;
			ondeleted?.();
		} finally {
			busy = false;
		}
	}
</script>

<AlertDialog.Root bind:open {onOpenChange}>
	<AlertDialog.Content>
		<AlertDialog.Title>Delete project {project}</AlertDialog.Title>
		<AlertDialog.Description>
			This revokes every grant on <span class="mono">project:{project}</span> and drops its registry record.
			It touches no bytes — a project delete has deliberately no cascade, so its warehouses and their
			buckets must be retired one rung at a time first.
		</AlertDialog.Description>

		{#if failure}
			<div class="failure" role="alert">
				{#if blockers.length > 0}
					<p class="lead">
						<TriangleAlert size={14} />
						{blockers.length} warehouse{blockers.length === 1 ? '' : 's'} still
						{blockers.length === 1 ? 'claims' : 'claim'} this project. Delete
						{blockers.length === 1 ? 'it' : 'each of them'} first — its bucket purge is a separate opt-in
						on that door:
					</p>
					<ul class="blockers">
						{#each blockers as id (id)}
							<li>
								<!-- Each blocking warehouse links to its own page: the fix the refusal hands the user. -->
								<a
									class="mono"
									href={resolve('/catalog/warehouses/[id]', { id: encodeURIComponent(id) })}
								>
									{id}<ExternalLink size={12} />
								</a>
							</li>
						{/each}
					</ul>
				{:else if gloss}
					<p class="lead">{gloss}</p>
				{/if}
				<p class="detail">{failure.detail}</p>
			</div>
		{/if}

		<!-- Offered when the server named the lock this turns — and, once armed, for as long as it STAYS
		     armed. `protectedRefusal` alone hid the box the moment any OTHER failure replaced the protected
		     one (a forced retry that 500s, or that meets the warehouse blockers the catalog only reveals
		     once protection is out of the way), leaving `force` on with no off-switch on screen: the button
		     still read "Force delete" and the next click still put ?force=true on the wire. An armed
		     destructive flag has to keep the control that disarms it. -->
		{#if protectedRefusal || force}
			<label class="force">
				<input
					type="checkbox"
					bind:checked={force}
					disabled={busy}
					aria-label="Override deletion protection"
				/>
				<span>
					Override deletion protection — the <span class="mono">protected</span> flag only, never authorization.
					The same project-admin gate runs with or without it.
				</span>
			</label>
		{/if}

		<div class="actions">
			<AlertDialog.Cancel disabled={busy}>
				{blockers.length > 0 ? 'Close' : 'Cancel'}
			</AlertDialog.Cancel>
			<!-- A plain Button, NOT AlertDialog.Action: the Action closes the dialog on click, and a 409
			     that names the blocking warehouses has to stay on screen to be useful. -->
			<!-- Named for the TENANT, not the noun: this label is the last thing read before an
			     irreversible action, and it is also what keeps it distinct from the page's own trigger. -->
			<Button variant="destructive" disabled={busy || blockers.length > 0} onclick={confirm}>
				{#if busy}…{:else if force}Force delete {project}{:else}Delete {project}{/if}
			</Button>
		</div>
	</AlertDialog.Content>
</AlertDialog.Root>

<style>
	.mono {
		font-family: var(--font-mono, ui-monospace, monospace);
	}
	.failure {
		display: flex;
		flex-direction: column;
		gap: 6px;
		font-size: 12px;
		border: 1px solid color-mix(in srgb, var(--destructive) 40%, var(--border));
		border-radius: var(--radius-sm);
		background: color-mix(in srgb, var(--destructive) 7%, transparent);
		padding: 9px 11px;
	}
	.lead {
		display: flex;
		align-items: center;
		gap: 6px;
		margin: 0;
		color: var(--foreground);
	}
	.detail {
		margin: 0;
		color: var(--muted-foreground);
	}
	.blockers {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.blockers a {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: var(--muted);
		color: var(--foreground);
		padding: 1px 7px;
		text-decoration: none;
	}
	.blockers a:hover {
		text-decoration: underline;
	}
	.force {
		display: flex;
		align-items: flex-start;
		gap: 7px;
		font-size: 12px;
		color: var(--muted-foreground);
	}
	.actions {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
	}
</style>
