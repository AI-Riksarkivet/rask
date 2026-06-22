<script lang="ts">
	import type { Snippet } from 'svelte';
	import * as Sidebar from '../components/sidebar/index.js';
	import AppSidebar from './app-sidebar.svelte';

	// The shared application shell: ONE grouped sidebar + a content inset. Every
	// microfrontend wraps its routes in this so they share identical chrome (no drift).
	// `pathname` comes from the consuming app's $app/state (the lib can't read it).
	let { pathname = '', children }: { pathname?: string; children: Snippet } = $props();
</script>

<Sidebar.Provider class="h-svh overflow-hidden">
	<AppSidebar {pathname} />
	<Sidebar.Inset class="overflow-hidden">
		{@render children()}
	</Sidebar.Inset>
</Sidebar.Provider>
