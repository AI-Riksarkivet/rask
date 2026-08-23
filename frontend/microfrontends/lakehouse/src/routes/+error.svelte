<script lang="ts">
	import { page } from '$app/state';
	import { AppError, ForbiddenPage } from '@rask/ui/shell';
	import { base } from '$app/paths';

	// THREE outcomes, not two — and conflating the first two cost a real debugging session.
	//
	// A 403 is an ANSWER: the estate-admin door threw it and the caller genuinely lacks the privilege.
	// A 401 is a DIFFERENT answer: there is no identity at all. Both used to render the 403 copy, so a
	// signed-OUT visitor was told their identity did not hold a privilege they had never presented an
	// identity for — while OpenFGA held the correct grant the whole time. On the one surface whose job
	// is explaining why access resolved as it did, that wording was actively misleading.
	const status = $derived(page.status);
	const forbidden = $derived(status === 403);
	const unauthenticated = $derived(status === 401);

	// The server encodes the attempted path after a `|` so sign-in can return here. Split rather than
	// parse: the message is human-facing first, and the suffix must never leak into the rendered copy.
	const parts = $derived((page.error?.message ?? '').split('|'));
	const message = $derived((parts[0] ?? '').trim());
	const attempted = $derived((parts[1] ?? '').trim());
	const loginHref = $derived(
		`${base}/auth/login?redirect=${encodeURIComponent(attempted || page.url.pathname)}`,
	);
</script>

{#if unauthenticated}
	<div class="mx-auto flex max-w-md flex-col items-center gap-4 px-6 py-24 text-center">
		<h1 class="text-xl font-semibold">{message || 'Sign in to continue'}</h1>
		<p class="text-sm text-muted-foreground">
			You are not signed in, so there is no identity to authorize. This is not a permissions problem
			— sign in and the estate will decide what you can reach.
		</p>
		<!-- data-sveltekit-reload: /auth/* is the HOME zone's BFF, not a route this zone owns, so a soft
		     navigation would 404 into this zone's own manifest. -->
		<a
			href={loginHref}
			data-sveltekit-reload
			class="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:opacity-90"
		>
			Sign in
		</a>
	</div>
{:else if forbidden}
	<ForbiddenPage
		title={message || 'Access denied'}
		message="These surfaces span every tenant. You ARE signed in — your identity simply does not hold the estate-admin privilege (can_observe_events on the FGA root)."
		home="/"
	/>
{:else}
	<AppError {status} message={page.error?.message} />
{/if}
