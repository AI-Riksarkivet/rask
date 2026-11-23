<script lang="ts">
	// Retiring a tenant: the project page's danger zone and the confirm it opens. Last on the page and
	// fenced off, because it is the one action there that cannot be undone by repeating it. The catalog
	// is the gate (project `can_administer`), so this renders for anyone who can READ the project and
	// the refusal is rendered honestly rather than the affordance being guessed at from an identity the
	// page does not hold.
	import { Trash2 } from '@lucide/svelte';
	import { Button } from '@riksarkivet/ui/button';
	import { resolve } from '$app/paths';
	import { goto } from '$app/navigation';
	import { DECLARED_IN_GIT_REASON } from '#lib/data/DeclaredInGit.svelte';
	import ProjectDeleteDialog from '#lib/ProjectDeleteDialog.svelte';

	let {
		project,
		warehouses,
		declared,
	}: {
		project: string;
		/** How many warehouses still claim the project: each is retired on its own rung first. */
		warehouses: number;
		/** Git declares the project, so its delete door answers 403. */
		declared: boolean;
	} = $props();

	let deleting = $state(false);
</script>

<section class="danger">
	<h2>Danger zone</h2>
	<p class="mut">
		Retiring <code class="mono">{project}</code> revokes every grant on it and drops its registry
		record. No bytes are touched: there is deliberately no cascade here, so its
		{warehouses === 1 ? 'warehouse' : 'warehouses'} — and the buckets behind
		{warehouses === 1 ? 'it' : 'them'} — have to be retired one rung at a time first.
	</p>
	{#if declared}
		<!-- Its delete door answers 403 for a project git declares, so the control stays on screen,
		     disabled, with the reason beside it. -->
		<p class="mut" id="project-git-reason">{DECLARED_IN_GIT_REASON}</p>
	{/if}
	<Button
		variant="destructive"
		size="sm"
		disabled={declared}
		aria-describedby={declared ? 'project-git-reason' : undefined}
		onclick={() => (deleting = true)}
	>
		<Trash2 size={14} /> Delete project
	</Button>
</section>

<ProjectDeleteDialog
	bind:open={deleting}
	{project}
	ondeleted={() => {
		// The project is gone — this very route's read would 404 on the next tick. Back to the
		// estate list, re-read from the server so the retired tenant is not still on it.
		goto(resolve('/projects'), { refreshAll: true });
	}}
/>

<style>
	.danger {
		border-top: 1px solid var(--border);
		padding-top: 16px;
		margin-bottom: 26px;
	}
	h2 {
		font-size: 14px;
		margin: 0 0 8px;
		color: var(--destructive);
	}
	.mut {
		color: var(--muted-foreground);
		font-size: 12px;
		margin: 4px 0 10px;
		max-width: 62ch;
	}
</style>
