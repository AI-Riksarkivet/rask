<script lang="ts">
	import type { Snippet } from 'svelte';

	// Content wrapper only. The top bar moved into the shared @rask/ui AppShell
	// (breadcrumb + sidebar trigger) and the sidebar footer (theme toggle + Ray
	// health). The 21 routes still pass legacy title/actions props; they are accepted
	// via the index signature but no longer rendered. Proper cleanup = drop RayShell
	// from those routes (they can render content directly inside AppShell) — a
	// route-ownership follow-up.
	interface Props {
		/** Drop the outer padding so content fills the inset (e.g. the canvas viewer). */
		flush?: boolean;
		children: Snippet;
		/** Legacy per-page top-bar props (title/center/actions/…) — accepted, not drawn. */
		[key: string]: unknown;
	}

	// `rest` absorbs the legacy top-bar props the 21 call sites still pass.
	// eslint-disable-next-line @typescript-eslint/no-unused-vars
	let { flush = false, children, ...rest }: Props = $props();
</script>

<main
	class={flush ? 'bg-background flex flex-1 overflow-hidden' : 'bg-background flex-1 overflow-auto'}
>
	{@render children()}
</main>
