<script lang="ts">
	// The estate's PROJECT GALLERY — ONE implementation, mounted by both `/` (the landing) and
	// `/projects` (the navbar-addressable surface). A second copy of this list is exactly the
	// duplication the 2026-08-03 ruling deletes: there is one project concept and it is the TOP of the
	// hierarchy (project › warehouse › namespace › table), so the estate's list of projects lives in
	// the main menu, never inside a project-scoped zone's catalog.
	import { Plus } from '@lucide/svelte';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { Card } from '@rask/ui/card';
	import { invalidateAll } from '$app/navigation';
	import { page } from '$app/state';
	import ProjectCreateDialog from '$lib/ProjectCreateDialog.svelte';
	import type { GalleryData } from '$lib/gallery';

	let {
		heading,
		authEnabled,
		signedIn,
		identityUnavailable,
		estateAdmin,
		meSubject,
		projects,
	}: GalleryData & {
		/** The h1 for the surface this is mounted on — the estate brand on `/`, "Projects" on
		 *  `/projects`. The only thing that differs between the two mounts. */
		heading: string;
		/** Whether the stack has OIDC configured at all (from the shared layout load) — an ungoverned
		 *  stack must not offer a dead sign-in link. */
		authEnabled: boolean;
	} = $props();

	let creating = $state(false);

	// Come back HERE after the OIDC round-trip, whichever of the two routes is mounted (the shell's
	// ?redirect= contract, navbar-user.svelte).
	const here = $derived(page.url.pathname);
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(here)}`);
</script>

<div class="px-4 py-10">
	<div class="mx-auto flex w-full max-w-5xl flex-col gap-6">
		<header class="flex flex-col gap-1">
			<div class="flex items-center justify-between gap-3">
				<h1 class="text-3xl font-semibold">{heading}</h1>
				{#if estateAdmin}
					<!-- Estate-admin only (the /v1/me gate). The create MINTS a project by provisioning its
					     first warehouse; the gallery reflects it via the invalidate `oncreated` triggers. -->
					<Button variant="outline" size="sm" onclick={() => (creating = true)}>
						<Plus /> New project
					</Button>
				{/if}
			</div>
			<p class="text-muted-foreground">
				Governed Lance lakehouse — {estateAdmin ? 'every project in the estate' : 'your projects'}.
			</p>
		</header>

		{#if projects.length > 0}
			<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
				{#each projects as p (p.project)}
					<!-- SAME-ZONE now: the project overview moved here with the list (2026-08-03), so this
					     is a soft nav and must NOT carry data-sveltekit-reload — the hard reload it used to
					     need was the cost of the page living in another zone. -->
					<a href={`/projects/${encodeURIComponent(p.project)}`} class="group block">
						<Card
							class="hover:border-ring/40 hover:bg-accent flex h-full flex-col gap-2 p-5 transition-colors"
						>
							<div class="flex items-start justify-between gap-2">
								<div class="truncate text-lg font-medium">{p.project}</div>
								{#if p.role}
									<Badge variant={p.role === 'admin' ? 'default' : 'secondary'}>{p.role}</Badge>
								{/if}
							</div>
							<div class="text-muted-foreground text-sm">
								{#if p.warehouses !== null}
									{p.warehouses}
									{p.warehouses === 1 ? 'warehouse' : 'warehouses'}
								{:else}
									project
								{/if}
							</div>
						</Card>
					</a>
				{/each}
			</div>
		{:else}
			<!-- Empty state: signed out (prompt), or signed in with no memberships. -->
			<div
				class="border-border flex flex-col items-center gap-3 rounded-lg border border-dashed p-10 text-center"
			>
				{#if signedIn}
					<p class="text-muted-foreground">
						You are not a member of any project yet. Ask a project admin for access.
					</p>
				{:else if identityUnavailable}
					<!-- Signed in, but the catalog could not confirm WHO. Offering "Sign in" here sends a
					     user who already signed in round a loop that cannot fix it — the fault is the
					     identity lookup, not the session. -->
					<p class="text-muted-foreground">
						You are signed in, but the catalog could not confirm your identity, so your projects cannot be
						listed. This is a backend fault, not a sign-in problem — retry, or check that the catalog is
						reachable.
					</p>
					<Button href={here}>Retry</Button>
				{:else if authEnabled}
					<p class="text-muted-foreground">Sign in to see your projects.</p>
					<Button href={loginHref}>Sign in</Button>
				{:else}
					<p class="text-muted-foreground">
						No projects to show — sign-in is not configured on this stack.
					</p>
				{/if}
			</div>
		{/if}
	</div>
</div>

<!-- Estate-admin only; `oncreated` invalidates the page load so the new project is on the very next
     frame of THIS gallery (the list is a load, not a query — see createWarehouse's note). -->
<ProjectCreateDialog
	bind:open={creating}
	defaultAdmin={meSubject}
	oncreated={() => void invalidateAll()}
/>
