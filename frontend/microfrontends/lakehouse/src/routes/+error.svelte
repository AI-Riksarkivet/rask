<script lang="ts">
	import { page } from '$app/state';
	import { AppError, ForbiddenPage } from '@rask/ui/shell';

	// A 403 is an ANSWER, not an error — the estate-admin door in admin/+layout.server.ts throws one,
	// and it must look identical to the client-side door in the root layout rather than like a crash.
	// Everything else is an app error.
	const forbidden = $derived(page.status === 403);
</script>

{#if forbidden}
	<ForbiddenPage
		title={page.error?.message ?? 'Access denied'}
		message="These surfaces span every tenant. Your identity does not hold the estate-admin privilege (can_observe_events on the FGA root)."
		home="/"
	/>
{:else}
	<AppError status={page.status} message={page.error?.message} />
{/if}
