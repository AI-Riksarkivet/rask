<script lang="ts">
	import { resolve } from '$app/paths';
	// The estate's PROJECT GALLERY, mounted by `/projects`. There is one project concept and it is the
	// TOP of the hierarchy (project › warehouse › namespace › table), so the estate's list of projects
	// lives in the main menu, never inside a project-scoped page's catalog.
	import { gsap } from 'gsap';
	import { LayoutGrid, List, Plus } from '@lucide/svelte';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { Card } from '@rask/ui/card';
	import { GatedAction } from '#lib/access/gated-action/index.ts';
	import * as Table from '@rask/ui/table';
	import { refreshAll } from '$app/navigation';
	import ProjectCreateDialog from '#lib/ProjectCreateDialog.svelte';
	import { Skeleton } from '@rask/ui/skeleton';
	import { page } from '$app/state';
	import { isEstateAdmin } from '#lib/estate-admin.ts';
	import { galleryOf } from '#lib/gallery.ts';
	import { fetchVisibleProjects } from '#lib/remote/warehouses.api.ts';

	/** The h1 for the surface this is mounted on. */
	let { heading }: { heading: string } = $props();

	// The identity is the root layout's; the estate listing is a read in the browser, so the page is
	// on screen at once and only the cards wait for a slow catalog. Read only for an estate admin:
	// anyone else's gallery is their memberships, and the listing would only add what they hold.
	const me = $derived(page.data.me ?? null);
	const listing = $derived(isEstateAdmin(me) ? fetchVisibleProjects() : null);
	const gallery = $derived(galleryOf(me, page.data.hasSession ?? false, listing?.current));
	const listingLoads = $derived(listing !== null && listing.current === undefined);
	// Whether the stack has OIDC configured at all: an ungoverned stack must not offer a dead sign-in link.
	const authEnabled = $derived(page.data.authEnabled ?? false);
	const signedIn = $derived(gallery.signedIn);
	const identityUnavailable = $derived(gallery.identityUnavailable);
	const estateAdmin = $derived(gallery.estateAdmin);
	const meSubject = $derived(gallery.meSubject);
	const projects = $derived(gallery.projects);

	/** A new project is on this gallery's next frame: the identity re-reads with the layout, and the
	 *  estate listing with its own read. */
	function created(): void {
		void refreshAll();
		void fetchVisibleProjects().refresh();
	}

	let creating = $state(false);

	/** The two views the projects list offers. */
	type ProjectsView = 'gallery' | 'table';

	// The view holds for this page only: no door keeps a per-person preference, so the toggle sends
	// nothing and a reload paints the default. Gallery by default: cards read well at the handful of
	// projects most people can see, and the table earns its place past that.
	let view = $state<ProjectsView>('gallery');

	function choose(next: ProjectsView): void {
		view = next;
	}

	// Come back to the gallery after the OIDC round-trip (the shell's ?redirect= contract,
	// navbar-user.svelte).
	const here = resolve('/projects');
	const loginHref = resolve(`auth/login?redirect=${encodeURIComponent(here)}`);

	// ONE-SHOT, and `entered` is what makes that literal. The grid node outlives a count change, so
	// adding a project (the create flow's `refreshAll`) already does not re-run the attachment —
	// but the view toggle DESTROYS the grid, and without the latch every trip through the table
	// would re-stagger cards the reader was already reading. Component-scoped, not module-scoped: a
	// fresh mount of the surface should still animate.
	//
	// Local rather than @rask/ui/motion's `stagger` because both differences are this gallery's own
	// (the latch, and the 0.98 scale), and that module is shared by every page.
	let entered = false;

	// Reduced motion: no tween, and nothing to undo — `from` never runs, so the cards are already at
	// their final state in the DOM.
	function cardsEnter(el: HTMLElement) {
		if (entered || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
		entered = true;
		const tween = gsap.from(el.children, {
			autoAlpha: 0,
			y: 16,
			scale: 0.98,
			duration: 0.5,
			stagger: 0.06,
			ease: 'power3.out',
			// Leave nothing inline: a lingering `transform` makes each card a containing block, and
			// the card's own hover transition has to own opacity once the entrance is done.
			clearProps: 'transform,opacity,visibility',
		});
		return () => tween.kill();
	}

	/** The empty state fades and nothing more — there is no list here to stagger. */
	function emptyEnter(el: HTMLElement) {
		if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
		const tween = gsap.from(el, {
			autoAlpha: 0,
			duration: 0.4,
			ease: 'power2.out',
			clearProps: 'opacity,visibility',
		});
		return () => tween.kill();
	}
</script>

<div class="px-4 py-10">
	<div class="mx-auto flex w-full max-w-5xl flex-col gap-6">
		<header class="flex flex-col gap-1">
			<div class="flex items-center justify-between gap-3">
				<h1 class="text-3xl font-semibold">{heading}</h1>
				<div class="flex items-center gap-2">
					{#if projects.length > 0}
						<!-- VIEW TOGGLE. A radiogroup, not two toggle buttons: exactly one view is active and
						     picking one deselects the other, which is what `radio` means and what a screen
						     reader then announces. Hidden when there is nothing to view — offering a way to
						     re-arrange an empty list is noise. -->
						<div
							role="radiogroup"
							aria-label="Project list view"
							class="border-border flex items-center gap-0.5 rounded-md border p-0.5"
						>
							{#each [{ id: 'gallery', label: 'Gallery', icon: LayoutGrid }, { id: 'table', label: 'Table', icon: List }] as const as option (option.id)}
								<button
									type="button"
									role="radio"
									aria-checked={view === option.id}
									aria-label={option.label}
									title={option.label}
									data-slot="projects-view-{option.id}"
									class={[
										'text-muted-foreground hover:text-foreground focus-visible:ring-ring rounded-sm p-1.5 transition-colors focus-visible:outline-none focus-visible:ring-2',
										view === option.id && 'bg-accent text-foreground',
									]}
									onclick={() => choose(option.id)}
								>
									<option.icon class="size-4" aria-hidden="true" />
								</button>
							{/each}
						</div>
					{/if}
					<!-- Estate-admin only (the /management/v1/me gate), and SHOWN disabled to everyone else (#143). The
					     create MINTS a project by provisioning its first warehouse; the gallery reflects it via
					     the invalidate `oncreated` triggers. Hiding the button taught a non-admin that projects
					     are not creatable in this product, when the truth is that THEY cannot create one — and
					     only the second version tells them whom to ask. -->
					<GatedAction
						allowed={estateAdmin}
						action="New project"
						reason="estate-admin only (needs can_observe_events on the FGA root)"
					>
						<Button variant="outline" size="sm" onclick={() => (creating = true)}>
							<Plus /> New project
						</Button>
					</GatedAction>
				</div>
			</div>
			<p class="text-muted-foreground">
				Governed Lance lakehouse — {estateAdmin ? 'every project in the estate' : 'your projects'}.
			</p>
		</header>

		{#if listingLoads}
			<!-- The estate listing is still out: the page is here, and its cards are on their way. -->
			<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3" data-testid="gallery-loading">
				{#each [0, 1, 2] as slot (slot)}
					<Skeleton class="h-24 rounded-xl" />
				{/each}
			</div>
		{:else if projects.length > 0 && view === 'table'}
			<!-- THE TABLE VIEW. Same rows, same links, same destinations as the cards — only denser.
			     It reads the SAME `projects` array rather than a second query: two views of one list,
			     not two lists. The whole row is not a link (an <a> cannot wrap <tr>), so the project
			     cell carries it and the rest of the row is plain text. -->
			<Table.Root>
				<Table.Header>
					<Table.Row>
						<Table.Head>Project</Table.Head>
						<Table.Head>Role</Table.Head>
						<Table.Head class="text-right">Warehouses</Table.Head>
					</Table.Row>
				</Table.Header>
				<Table.Body>
					{#each projects as p (p.project)}
						<Table.Row>
							<Table.Cell class="font-medium">
								<!-- An in-app link: the overview soft-navigates. -->
								<a
									href={resolve('/projects/[project]', { project: encodeURIComponent(p.project) })}
									class="hover:underline"
								>
									{p.project}
								</a>
							</Table.Cell>
							<Table.Cell>
								{#if p.role}
									<Badge variant={p.role === 'admin' ? 'default' : 'secondary'}>{p.role}</Badge>
								{:else}
									<span class="text-muted-foreground">—</span>
								{/if}
							</Table.Cell>
							<Table.Cell class="text-muted-foreground text-right">
								{p.warehouses ?? '—'}
							</Table.Cell>
						</Table.Row>
					{/each}
				</Table.Body>
			</Table.Root>
		{:else if projects.length > 0}
			<!-- The entrance is attached to the CONTAINER, not each card: one tween staggering the
			     grid's own children, so a card added later slots in without its own animation and
			     without restarting anyone else's. -->
			<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3" {@attach cardsEnter}>
				{#each projects as p (p.project)}
					<!-- An in-app link: the project overview soft-navigates. -->
					<a
						href={resolve('/projects/[project]', { project: encodeURIComponent(p.project) })}
						class="group block"
					>
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
				{@attach emptyEnter}
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
						You are signed in, but the catalog could not confirm your identity, so your projects
						cannot be listed. This is a backend fault, not a sign-in problem — retry, or check that
						the catalog is reachable.
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

<!-- Estate-admin only; `oncreated` re-reads what the gallery is drawn from. -->
<ProjectCreateDialog bind:open={creating} defaultAdmin={meSubject} oncreated={created} />
