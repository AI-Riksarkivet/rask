<script lang="ts">
	import { page } from '$app/state';
	import { AppShell } from '@rask/ui/shell';
	import type { Snippet } from 'svelte';
	let { children }: { children: Snippet } = $props();
</script>

<!-- In-project shell: the shared @rask/ui AppShell (grouped sidebar + breadcrumb).
     The active project is the [project] route param; nav hrefs + breadcrumb derive
     from the full pathname (/<project>/<domain>/…). -->
<AppShell
	pathname={page.url.pathname}
	project={{ name: page.params.project ?? 'default', subtitle: 'Project' }}
>
	{@render children()}
</AppShell>
