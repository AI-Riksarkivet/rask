<script lang="ts">
	// One rendered identity. The helper's contract is that the FULL subject always rides `title`, so
	// hover and copy still yield the value a tuple write needs; a component makes that structural, since
	// there is no way to render a subject here without its title.
	//
	// A module of its own rather than part of the grants panel: rendering an OIDC sub a human can read
	// is the concern of FGA subjects, control-event actors, a dropped-by stamp and any audit surface.
	import { subjectDisplay } from './subject.ts';

	let {
		value,
		fallback = 'system',
		class: className = '',
	}: {
		/** The raw subject — an FGA subject, an OIDC sub, or `null` for a system/unattributed action. */
		value: string | null | undefined;
		/** What to render when there is no subject at all. `system` reads correctly for control events. */
		fallback?: string;
		class?: string;
	} = $props();

	const shown = $derived(subjectDisplay(value ?? fallback));
</script>

<span class="subject mono {className}" title={shown.title}>{shown.label}</span>

<style>
	.subject {
		font-family: ui-monospace, monospace;
	}
</style>
