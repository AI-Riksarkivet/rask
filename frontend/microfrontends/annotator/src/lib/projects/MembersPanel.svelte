<script lang="ts">
	// Who may do what on this project. The rungs already existed in the model, concentric and
	// directly assignable — what did not exist was any way to grant one, so a project's creator got
	// `owner` at create and nobody else could be added. Every collaborative project therefore needed
	// someone with direct store access, which is the shape where people end up sharing an account.
	//
	// `can_manage`-gated server-side on READ as well as write: who has access to a thing names the
	// people worth phishing.
	import { Trash2, UserPlus } from '@lucide/svelte';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { Subject } from '@rask/ui/identity';
	import { Input } from '@rask/ui/input';
	import { Select } from '@rask/ui/select';

	import { fetchMembers, grantMember, revokeMember } from './remote/projects.remote';

	let { projectId }: { projectId: string } = $props();

	let members = $state<{ user: string; relation: string }[]>([]);
	/** Supplied by the SERVER so this never keeps a second copy of the model's ladder. */
	let grantable = $state<string[]>([]);
	let loaded = $state(false);
	/** The READ failed — there is no list to show, so the error replaces it. */
	let loadError = $state('');
	/** A grant or revoke failed — the list is still valid and must STAY, with the refusal beside it.
	 *
	 *  One `error` for both conflated them: a refused revoke hid the very list the manager needs to
	 *  act on the refusal ("grant another owner first" is impossible if you cannot see who there is).
	 *  Caught by the e2e, not by looking. */
	let actionError = $state('');
	let who = $state('');
	let rung = $state('annotator');
	let busy = $state(false);

	async function load(): Promise<void> {
		const result = await fetchMembers({ projectId });
		loaded = true;
		if (!result.ok) {
			// The server's words. A 403 here means the viewer is not a manager, which is a different
			// thing from "this project has no members" and must not render as an empty list.
			loadError = result.detail;
			return;
		}
		loadError = '';
		members = result.data.members;
		grantable = result.data.grantable;
		if (grantable.length > 0 && !grantable.includes(rung)) rung = grantable[0]!;
	}

	async function grant(): Promise<void> {
		if (busy || !who.trim()) return;
		busy = true;
		const result = await grantMember({ projectId, user: who.trim(), relation: rung });
		busy = false;
		if (result.ok) {
			members = result.data.members;
			who = '';
			actionError = '';
		} else actionError = result.detail;
	}

	async function revoke(user: string, relation: string): Promise<void> {
		if (busy) return;
		busy = true;
		const result = await revokeMember({ projectId, user, relation });
		busy = false;
		// The refusal that matters arrives here: removing the last owner/manager is a named 409, not
		// a silent no-op, because a project nobody can administer cannot be undone from inside.
		if (result.ok) {
			members = result.data.members;
			actionError = '';
		} else actionError = result.detail;
	}
</script>

<section class="flex flex-col gap-2" data-testid="members-panel">
	<div class="flex items-center gap-2">
		<h2 class="text-muted-foreground text-xs font-medium uppercase">Access</h2>
		{#if !loaded}
			<Button variant="ghost" size="sm" data-testid="load-members" onclick={() => void load()}>
				Show who has access
			</Button>
		{/if}
	</div>

	{#if loadError || actionError}
		<p class="text-destructive text-sm" data-testid="members-error">{loadError || actionError}</p>
	{/if}

	<!-- Gated on the LOAD only. A refused action leaves the list intact, because acting on the
	     refusal ("grant another owner first") requires seeing who there is. -->
	{#if loaded && !loadError}
		<ul class="flex flex-col gap-1">
			{#each members as m (m.user + m.relation)}
				<li class="flex items-center gap-2 text-sm" data-testid="member-row">
					<!-- An OIDC `sub` is an opaque base64 blob — printed raw it fills the row with
					     `user:CiQwOGE4Njg0Yi1kYjg4…` and tells nobody anything. `Subject` ellipsizes the
					     opaque ones in the MIDDLE (keeping both ends, which is what distinguishes two
					     subjects) while leaving readable identifiers verbatim, and always carries the FULL
					     value on `title` so hover and copy still yield what a tuple write needs. Never
					     `subjectDisplay(...).label` — the component is what makes that title invariant
					     structural. -->
					<Subject value={m.user} class="text-xs" />
					<Badge variant="outline">{m.relation}</Badge>
					<Button
						variant="ghost"
						size="icon-sm"
						class="ml-auto"
						disabled={busy}
						data-testid="revoke-member"
						title="revoke {m.relation} from {m.user}"
						onclick={() => void revoke(m.user, m.relation)}
					>
						<Trash2 class="size-3.5" />
					</Button>
				</li>
			{:else}
				<li class="text-muted-foreground text-sm">No direct grants yet.</li>
			{/each}
		</ul>

		<form
			class="flex flex-wrap items-center gap-2"
			onsubmit={(e) => {
	e.preventDefault();
	void grant();
}}
		>
			<Input
				bind:value={who}
				placeholder="annotator (OIDC subject or username)"
				aria-label="Person to grant"
				class="h-8 w-56"
			/>
			<Select
				bind:value={rung}
				ariaLabel="Rung"
				options={grantable.map((g) => ({ value: g, label: g }))}
			/>
			<Button type="submit" size="sm" disabled={busy || !who.trim()} data-testid="grant-member">
				<UserPlus class="size-3.5" /> Grant
			</Button>
		</form>
		<!-- Said out loud: a tenant admin reaches this project through `admin from tenant`, is not a
		     direct grant, and cannot be revoked here. Listing them with a remove button that cannot
		     work would be worse than not listing them. -->
		<p class="text-muted-foreground text-xs">
			Direct grants only. Tenant admins reach this project through the tenant and are managed there.
		</p>
	{/if}
</section>
