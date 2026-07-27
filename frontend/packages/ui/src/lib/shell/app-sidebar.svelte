<script lang="ts">
	import * as Sidebar from '../components/sidebar/index.js';
	import ProjectSwitcher from './project-switcher.svelte';
	import NavMain from './nav-main.svelte';
	import NavUser from './nav-user.svelte';
	import type { Project, NavUser as NavUserData } from './nav-config.js';

	// The shadcn sidebar-07 structure: collapsible-to-icon sidebar with a project
	// switcher (header), an accordion NavMain (content), and a profile dropdown
	// (footer). Pure shared shell — `pathname` comes from the app's $app/state.
	let {
		pathname = '',
		project,
		user,
	}: {
		pathname?: string;
		project?: Project;
		user?: NavUserData;
	} = $props();
</script>

<Sidebar.Root collapsible="icon">
	<Sidebar.Header>
		<ProjectSwitcher {project} />
	</Sidebar.Header>
	<Sidebar.Content>
		<NavMain {pathname} />
	</Sidebar.Content>
	<Sidebar.Footer>
		<NavUser {user} />
	</Sidebar.Footer>
	<Sidebar.Rail />
</Sidebar.Root>
