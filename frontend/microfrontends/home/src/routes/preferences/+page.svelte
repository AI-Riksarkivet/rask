<script lang="ts">
	import { Bell } from '@lucide/svelte';

	// PREFERENCES — the per-SUBJECT level, and the third thing the estate configures.
	//
	//   · a PROJECT's settings are about one tenant's data
	//   · `/settings` is about the INSTALLATION — platform-scoped, estate-admin only, server-gated
	//   · this is about YOU, and every signed-in user has it
	//
	// It exists because the second and third were briefly the same place. The notification page landed
	// under `/settings/notifications`, behind a layout load that 404s any non-admin — so a feature
	// belonging to everyone was reachable by almost nobody. Splitting the level is the fix; relaxing
	// that guard for one child would have made a structural door a remembered one.
	//
	// NO SERVER GATE HERE, and that is correct rather than an omission: there is nothing to authorize.
	// Every read below is subject-derived at the service (the inbox actor id is minted from the
	// verified token sub, with no header fallback), so an anonymous visitor sees the honest
	// "unavailable on this stack" state rather than anyone else's preferences.

	const ROW =
		'hover:bg-accent/40 focus-visible:ring-ring flex items-center gap-3 rounded-lg border p-4 transition-colors focus-visible:ring-2 focus-visible:outline-none';
</script>

<svelte:head><title>Preferences · lance</title></svelte:head>

<div class="mx-auto flex w-full max-w-3xl flex-col gap-6 p-6">
	<header class="flex flex-col gap-1">
		<h1 class="text-2xl font-semibold">Preferences</h1>
		<p class="text-muted-foreground text-sm">
			Yours, not the platform's. These follow your account across every zone.
		</p>
	</header>

	<!-- Written out literally, never from a loop: `@rask/zone-contract`'s link gates read hrefs off
	     Svelte's AST and render any `{…}` expression as an opaque placeholder, so a looped href is
	     invisible to both the cross-zone-reload and link-target checks. -->
	<a href="/preferences/notifications" class={ROW}>
		<Bell class="size-5 shrink-0" aria-hidden="true" />
		<span class="flex min-w-0 flex-col">
			<span class="font-medium">Notifications</span>
			<span class="text-muted-foreground text-xs">
				Email and Slack delivery, digest batching, and the projects you watch.
			</span>
		</span>
	</a>
</div>
