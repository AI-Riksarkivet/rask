<script lang="ts">
	import { Badge } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import {
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
	// Two kinds of row, and the difference is deliberate and visible:
	//   · AUTH/AUTHZ rows link to surfaces that already EXIST — access, tenants and the audit trail,
	//     still served by the lakehouse app at their own addresses. They moved here in the IA only;
	//     the routes did not move, so these are cross-zone links and carry data-sveltekit-reload.
	//     Re-basing them under `/settings/` is a separate change that needs redirects.
	//   · NOTIFICATIONS and PROJECT DEFAULTS are named but NOT wired, and say so. Each needs a store
	//     that does not exist yet; a settings page that silently discards what you type is worse than
	//     one that admits it cannot save.
	let { data }: { data: PageData } = $props();

	type Row = {
		title: string;
		blurb: string;
		icon: typeof ShieldCheck;
		href?: string;
		needs?: string;
	};

	const AUTH: Row[] = [
		{
			title: 'Access',
			blurb: 'Who may do what — the FGA workbench over the estate’s tuples.',
			icon: ShieldCheck,
			href: '/lakehouse/governance/access',
		},
		{
			title: 'Tenants',
			blurb: 'The projects that exist, their warehouses and their members.',
			icon: Building2,
			href: '/lakehouse/admin/tenants',
		},
		{
			title: 'Audit',
			blurb: 'What was done, by whom, and whether it was allowed.',
			icon: ScrollText,
			href: '/lakehouse/governance/audit',
		},
	];

	const UNWIRED: Row[] = [
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
</script>

<svelte:head><title>Settings · lance</title></svelte:head>

<div class="mx-auto flex w-full max-w-3xl flex-col gap-6 p-6">
	<header class="flex flex-col gap-1">
		<h1 class="text-2xl font-semibold">Settings</h1>
		<p class="text-muted-foreground text-sm">
			Estate-wide configuration. Signed in as an estate admin — everything here affects every project.
		</p>
	</header>

	<section class="flex flex-col gap-2" aria-label="Access and authorization">
		<h2 class="text-muted-foreground text-xs font-semibold uppercase tracking-wide">
			Access &amp; authorization
		</h2>
		{#each AUTH as row (row.title)}
			<!-- CROSS-ZONE: these pages are served by the lakehouse app, so a soft nav would land in a
			     route this zone does not own (404). data-sveltekit-reload is mandatory. -->
			<a
				href={row.href}
				data-sveltekit-reload
				class="hover:bg-accent/40 focus-visible:ring-ring flex items-center gap-3 rounded-lg border p-4 transition-colors focus-visible:outline-none focus-visible:ring-2"
			>
				<row.icon class="size-5 shrink-0" aria-hidden="true" />
				<span class="flex flex-col">
					<span class="font-medium">{row.title}</span>
					<span class="text-muted-foreground text-xs">{row.blurb}</span>
				</span>
			</a>
		{/each}
	</section>

	<section class="flex flex-col gap-2" aria-label="Estate preferences">
		<h2 class="text-muted-foreground text-xs font-semibold uppercase tracking-wide">Preferences</h2>
		{#each UNWIRED as row (row.title)}
			<Card class="flex items-center gap-3 p-4">
				<row.icon class="text-muted-foreground size-5 shrink-0" aria-hidden="true" />
				<span class="flex min-w-0 flex-col">
					<span class="font-medium">{row.title}</span>
					<span class="text-muted-foreground text-xs">{row.blurb}</span>
					<span class="text-muted-foreground/80 text-xs">Waiting on {row.needs}.</span>
				</span>
				<Badge variant="outline" class="ml-auto shrink-0">Not wired</Badge>
			</Card>
		{/each}
	</section>

	<p class="text-muted-foreground/80 text-xs">
		Signed in as {data.meSubject || 'an unresolved identity'}.
	</p>
</div>
