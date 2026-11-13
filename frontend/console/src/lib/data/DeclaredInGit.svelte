<script lang="ts" module>
	/** Whether git declares a project or warehouse record (XC-213): the catalog then converges it from
	 *  the chart's `catalog.declared` values and refuses its lifecycle doors (deactivate, activate,
	 *  protection, delete) with a 403, so a page offers none of them. */
	export function declaredInGit(
		record: { managed_by?: string | null } | null | undefined,
	): boolean {
		return record?.managed_by === 'git';
	}

	/** Why a control on a git-declared object is disabled, in the words a disabled control carries. */
	export const DECLARED_IN_GIT_REASON =
		'Its lifecycle is changed in git’s declaration (the chart’s catalog.declared values); the catalog refuses it here.';
</script>

<script lang="ts">
	// The plain marker of a git-declared project or warehouse. Its `id` lets a disabled control name it
	// as its description, so the reason reaches a keyboard and a screen reader, not only a hover.
	import { Badge } from '@rask/ui/badge';

	let { id }: { id?: string } = $props();
</script>

<Badge variant="outline" {id} title={`Declared in git. ${DECLARED_IN_GIT_REASON}`}
	>Declared in git<span class="sr-only">. {DECLARED_IN_GIT_REASON}</span></Badge
>
