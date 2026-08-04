<script lang="ts">
	// One rendered identity (#87). The helper alone was not enough: its contract is "the FULL subject
	// always rides `title`, so hover and copy still yield the value a tuple write needs" — and a call
	// site that reached for `.label` alone silently dropped exactly that (TableDetail's recovery panel
	// did, within days of the helper shipping). A component makes the invariant STRUCTURAL: there is no
	// way to render a subject here without its title.
	//
	// Estate-wide by design, which is why it lives at `@rask/ui/identity` and not under grants-panel —
	// the concern is "render an OIDC sub a human can read", and it applies to FGA subjects, control-event
	// actors, a dropped-by stamp, and any future audit surface. None of those are grants panels.
	import { subjectDisplay } from './subject.js';

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
