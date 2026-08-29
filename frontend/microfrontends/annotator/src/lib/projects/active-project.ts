import { projectFromHost } from '@rask/ui/shell';

/**
 * The ACTIVE PROJECT — the tenant every projects surface in this zone acts on.
 *
 * Resolved by the SAME rule the shared shell resolves it by (`@rask/ui`'s `app-shell.svelte` and
 * `project-switcher.svelte`): a host-scoped deploy names the project in the request host, otherwise
 * it is the `rask_active_project` cookie (#103) that `zoneLayoutLoad` surfaces to every zone as
 * `data.activeProject`. One rule, so the page and the chrome above it can never disagree.
 *
 * AN EMPTY STRING MEANS NO PROJECT IS ACTIVE, and the caller must say so. There is deliberately no
 * fallback: this used to end `?? me.projects[0].project ?? 'default'`, taking the caller's first
 * membership BY ARRAY POSITION and then a literal. Both invent a tenant nobody chose — with nothing
 * entered the shell correctly read "No active project" while the landing listed, and created into,
 * whichever project sorted first.
 */
export function activeTenant(host: string, activeProject: string): string {
	return projectFromHost(host) ?? activeProject;
}
