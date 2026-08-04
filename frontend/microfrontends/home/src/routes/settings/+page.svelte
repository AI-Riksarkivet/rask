<script lang="ts">
	import { Badge } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import {
		ArrowUpRight,
		Bell,
		KeyRound,
		ScrollText,
		ShieldCheck,
		SlidersHorizontal,
		Building2,
	} from '@lucide/svelte';
	import type { PageData } from './$types';

	// SETTINGS — what configures the ESTATE, as opposed to what any one zone does. The third place in
	// the main menu, and estate-admin only: the gate is in `+page.server.ts`, because hiding the
	// navbar entry is presentation, not authorization.
	//
	// Two kinds of row, and the difference is deliberate and VISIBLE on the row itself:
	//
	//   · AUTH/AUTHZ rows link to surfaces that already exist and are genuinely estate-scoped — the
	//     FGA workbench reads the whole tuple store, Tenants IS the estate's project list, and the
	//     audit trail is gated on estate-admin and takes no project. None of the three accepts a
	//     project parameter, so none of them belongs inside a project. They are still SERVED by the
	//     lakehouse app at their old addresses, which makes each one a cross-zone hop that swaps the
	//     main menu for the lakehouse's own navigation — so each row SAYS SO, in the badge and in the
	//     section's own copy. A settings page that teleports you into another app's chrome without a
	//     word is the defect this text exists to close; re-basing the routes under `/settings/` is the
	//     real fix and is a port, not a link change (see the note on the hrefs below).
	//
	//   · NOTIFICATIONS, NEW-PROJECT DEFAULTS and CREDENTIALS are named but NOT built, and say which
	//     missing thing blocks each. They are deliberately not controls at all — no toggle, no field,
	//     nothing to click — because a settings form that silently discards what you type is worse
	//     than one that admits it does not exist yet.
	let { data }: { data: PageData } = $props();

	/** Every lucide icon shares one component signature, so any icon's type fits. */
	type IconComponent = typeof ShieldCheck;

	type Unwired = { title: string; blurb: string; icon: IconComponent; needs: string };

	// The rows that are NOT built. Each carries its blocker, and the blocker is the point: "waiting on
	// a store that does not exist" is a decision someone made, whereas a dead control is a bug.
	const UNWIRED: Unwired[] = [
		{
			title: 'Notifications',
			blurb: 'Which run and cascade events raise the bell, and who they reach.',
			icon: Bell,
			needs: 'a per-subject preference document beside the existing dock-layout state',
		},
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

	const CROSS_ZONE_ROW =
		'hover:bg-accent/40 focus-visible:ring-ring flex items-center gap-3 rounded-lg border p-4 transition-colors focus-visible:outline-none focus-visible:ring-2';
</script>

<!-- The body of one cross-zone row. Only the CHROME is shared: each row's `<a href>` is written out
     literally below, never `{row.href}` from a loop. That is not a style choice — `@rask/zone-contract`'s
     cross-zone-reload gate reads hrefs off Svelte's own AST and renders any `{…}` expression as an
     opaque placeholder, so a looped href is INVISIBLE to it and `data-sveltekit-reload` would once
     again be correct only because a human remembered. Written literally, both gates see these three:
     the reload gate proves they hard-navigate, and `link-targets` proves `/lakehouse` is a segment the
     estate actually serves.

     Named for its ONE destination, not `crossZoneRow`: the badge text below is a literal, so a
     generically-named snippet would invite the next row — to `/explorer`, say — to reuse it and
     announce the wrong app. A second destination gets a second snippet, or a parameter. -->
{#snippet lakehouseRow(Icon: IconComponent, title: string, blurb: string)}
	<Icon class="size-5 shrink-0" aria-hidden="true" />
	<span class="flex min-w-0 flex-col">
		<span class="font-medium">{title}</span>
		<span class="text-muted-foreground text-xs">{blurb}</span>
	</span>
	<!-- Inside the link on purpose: it lands in the link's ACCESSIBLE NAME, so the destination is
	     announced rather than only drawn. -->
	<Badge variant="outline" class="ml-auto shrink-0">
		<ArrowUpRight aria-hidden="true" />
		Opens in Lakehouse
	</Badge>
{/snippet}

<svelte:head><title>Settings · lance</title></svelte:head>

<div class="mx-auto flex w-full max-w-3xl flex-col gap-6 p-6">
	<header class="flex flex-col gap-1">
		<h1 class="text-2xl font-semibold">Settings</h1>
		<p class="text-muted-foreground text-sm">
			Estate-wide configuration. Signed in as an estate admin — everything here affects every project.
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
			Estate-scoped — each one reads across every project and none of them takes a project. They are
			still <strong class="font-medium">served by the Lakehouse app</strong>, so opening one leaves
			this page: the main menu is replaced by the Lakehouse navigation until you come back. Moving them
			under <code class="text-[0.95em]">/settings/</code> is a port of the pages themselves, not a change
			of link.
		</p>
		<a href="/lakehouse/governance/access" data-sveltekit-reload class={CROSS_ZONE_ROW}>
			{@render lakehouseRow(
				ShieldCheck,
				'Access',
				'Who may do what — the FGA workbench over the estate’s whole tuple store.',
			)}
		</a>
		<a href="/lakehouse/admin/tenants" data-sveltekit-reload class={CROSS_ZONE_ROW}>
			{@render lakehouseRow(
				Building2,
				'Tenants',
				'Every project the estate knows, its warehouses and their effective admins.',
			)}
		</a>
		<a href="/lakehouse/governance/audit" data-sveltekit-reload class={CROSS_ZONE_ROW}>
			{@render lakehouseRow(
				ScrollText,
				'Audit',
				'What was done, by whom, and whether it was allowed.',
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
