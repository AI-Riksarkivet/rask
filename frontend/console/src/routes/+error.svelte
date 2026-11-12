<script lang="ts">
	import { page } from '$app/state';
	import { AppError } from '@rask/ui/shell';

	/** What a status means to a reader, when the refusal named nothing more specific. */
	function headingFor(status: number): string {
		if (status === 403) return 'You don’t have access to this page';
		if (status === 404) return 'Page not found';
		if (status === 503) return 'The console is unavailable';
		return 'Something went wrong';
	}

	const heading = $derived(page.error?.title ?? headingFor(page.status));
</script>

<!-- Its own title, or the tab keeps the page before it. -->
<svelte:head><title>{heading} · rask</title></svelte:head>

<AppError status={page.status} {heading} message={page.error?.message} />
