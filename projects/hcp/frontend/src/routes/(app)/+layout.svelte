<script lang="ts">
	import * as Sidebar from '$lib/components/ui/sidebar/index.js';
	import AppSidebar from '$lib/components/layout/AppSidebar.svelte';
	import AppHeader from '$lib/components/layout/AppHeader.svelte';
	import NavigationProgress from '$lib/components/layout/NavigationProgress.svelte';

	let { data, children } = $props();
</script>

<NavigationProgress />
<Sidebar.Provider>
	<AppSidebar accessLevel={data.accessLevel} />
	<Sidebar.Inset>
		<AppHeader
			username={data.username}
			tenant={data.tenant}
			userGUID={data.userGUID}
			sessions={data.sessions}
		/>
		<div class="flex-1 overflow-y-auto p-6">
			{@render children()}
		</div>
	</Sidebar.Inset>
</Sidebar.Provider>
