<script lang="ts">
	import { Badge } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import {
		Bell,
		FolderKanban,
		KeyRound,
		ScrollText,
		SlidersHorizontal,
		Users,
	} from '@lucide/svelte';
	import type { PageData } from './$types';

	// SETTINGS — the PLATFORM level: what configures the whole installation, as opposed to what any one
	// project or zone does. The third place in the main menu, and estate-admin only: the gate is in
	// `+layout.server.ts` (it covers this page AND its children), because hiding the navbar entry is
	// presentation, not authorization.
	//
	// Two kinds of row, and the difference is deliberate and VISIBLE on the row itself:
	//
	//   · The PLATFORM surfaces — users & roles, projects, the audit trail — are genuinely
	//     platform-scoped: each reads across every project and none of them accepts a project
	//     parameter, so none belongs inside one. They are SERVED HERE now (#105): the FGA workbench is
	//     `/settings/access` and the audit trail `/settings/audit`, both routes of this app, and the
	//     projects list is `/projects`, which this app has always owned. All three are same-zone soft
	//     navigations — no badge, no document load, no swap into another app's chrome. The lakehouse
	//     keeps only its PER-OBJECT grants plane (the access tab on one table or namespace), which is a
	//     different question at a different level.
	//
	//   · NOTIFICATIONS, NEW-PROJECT DEFAULTS and CREDENTIALS are named but NOT built, and say which
	//     missing thing blocks each. They are deliberately not controls at all — no toggle, no field,
	//     nothing to click — because a settings form that silently discards what you type is worse
	//     than one that admits it does not exist yet.
	let { data }: { data: PageData } = $props();

	/** Every lucide icon shares one component signature, so any icon's type fits. */
	type IconComponent = typeof Users;

	type Unwired = { title: string; blurb: string; icon: IconComponent; needs: string };

	// The rows that are NOT built. Each carries its blocker, and the blocker is the point: "waiting on
	// a store that does not exist" is a decision someone made, whereas a dead control is a bug.
	const UNWIRED: Unwired[] = [
		{
			title: 'New-project defaults',
			blurb: 'What a project is created WITH — storage tier, retention, initial admin.',
			icon: SlidersHorizontal,
			needs: 'the catalog to accept a defaults payload on project creation',
		},
		{
			title: 'Credentials',
			blurb: 'OIDC issuer and client, and the secrets the estate reads at boot.',
			icon: KeyRound,
			needs: 'the OpenBao-backed secret store — never editable from a browser form',
		},
	];

	const PLATFORM_ROW =
		'hover:bg-accent/40 focus-visible:ring-ring flex items-center gap-3 rounded-lg border p-4 transition-colors focus-visible:outline-none focus-visible:ring-2';
</script>

<!-- The body of one platform row. Only the CHROME is shared: each row's `<a href>` is written out
     literally below, never `{row.href}` from a loop. That is not a style choice — `@rask/zone-contract`'s
     cross-zone-reload and link-targets gates read hrefs off Svelte's own AST and render any `{…}`
     expression as an opaque placeholder, so a looped href is INVISIBLE to both, and the day one of these
     leaves the zone again `data-sveltekit-reload` would be correct only because a human remembered.

     No "Opens in Lakehouse" badge any more, and no snippet named for one destination: every row here is
     a route this app serves, so there is no other app to announce. -->
{#snippet platformRow(Icon: IconComponent, title: string, blurb: string)}
	<Icon class="size-5 shrink-0" aria-hidden="true" />
	<span class="flex min-w-0 flex-col">
		<span class="font-medium">{title}</span>
		<span class="text-muted-foreground text-xs">{blurb}</span>
	</span>
{/snippet}

<svelte:head><title>Settings · lance</title></svelte:head>

<div class="mx-auto flex w-full max-w-3xl flex-col gap-6 p-6">
	<header class="flex flex-col gap-1">
		<h1 class="text-2xl font-semibold">Settings</h1>
		<p class="text-muted-foreground text-sm">
			Platform-wide configuration. Signed in as an estate admin — everything here affects every
			project.
		</p>
	</header>

	<section class="flex flex-col gap-2" aria-labelledby="settings-authz">
		<h2
			id="settings-authz"
			class="text-muted-foreground text-xs font-semibold uppercase tracking-wide"
		>
			Access &amp; authorization
		</h2>
		<p class="text-muted-foreground text-xs">
			Platform-scoped — each one reads across every project and none of them takes a project. All three
			are served by this app, so opening one keeps the main menu: they are ordinary in-app navigations,
			not hops into another app's chrome.
		</p>
		<a href="/settings/access" class={PLATFORM_ROW}>
			{@render platformRow(
				Users,
				'Users & roles',
				'Who may do what, platform-wide — the FGA workbench over the whole tuple store.',
			)}
		</a>
		<a href="/projects" class={PLATFORM_ROW}>
			{@render platformRow(
				FolderKanban,
				'Projects',
				'Every project the platform knows, its warehouses and their effective admins.',
			)}
		</a>
		<a href="/settings/audit" class={PLATFORM_ROW}>
			{@render platformRow(
				ScrollText,
				'Audit',
				'The platform trail — what was done, by whom, and whether it was allowed.',
			)}
		</a>
	</section>

	<section class="flex flex-col gap-2" aria-labelledby="settings-preferences">
		<h2
			id="settings-preferences"
			class="text-muted-foreground text-xs font-semibold uppercase tracking-wide"
		>
			Preferences
		</h2>
		<p class="text-muted-foreground text-xs">
			Named here, and deliberately not built yet — each is blocked on something that does not exist,
			and the row says which. There is no control on any of them to be broken.
		</p>
		{#each UNWIRED as row (row.title)}
			<Card class="flex items-center gap-3 p-4">
				<row.icon class="text-muted-foreground size-5 shrink-0" aria-hidden="true" />
				<span class="flex min-w-0 flex-col">
					<span class="font-medium">{row.title}</span>
					<span class="text-muted-foreground text-xs">{row.blurb}</span>
					<span class="text-muted-foreground/80 text-xs">Blocked on {row.needs}.</span>
				</span>
				<Badge variant="outline" class="ml-auto shrink-0">Not wired yet</Badge>
			</Card>
		{/each}
	</section>

	<p class="text-muted-foreground/80 text-xs">
		Signed in as {data.meSubject || 'an unresolved identity'}.
	</p>
</div>
