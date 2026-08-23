<script lang="ts">
	// Annotation settings — planned, not built. See `PlannedArea` for why a stub says so loudly.
	//
	// The gap this names is real and specific: every setting the zone HAS is per-project and reachable
	// only from inside one. `EditOntologyDialog` edits an ontology from a project page;
	// `consensus_n` is a project field. So there is no answer to "what do all my projects agree on"
	// and no way to define a label set once and reuse it — every campaign re-types its own, and two
	// campaigns that meant the same class end up with two names that no scorer can reconcile.
	import PlannedArea from '$lib/nav/PlannedArea.svelte';
</script>

<PlannedArea
	title="Annotation settings"
	summary="Defaults that apply across projects, rather than being re-typed inside each one."
	intent={[
		'Reusable label ontologies — define a class set once and bind projects to it, instead of each project owning a private copy that silently diverges.',
		'Zone defaults for a new project: consensus_n, whether review is required, and which assist producer pre-fills.',
		'Keyboard bindings for the canvas tools, which are currently fixed in tool-defs.ts and cannot be matched to an existing team habit.',
		'Where the per-project settings that already exist (the ontology editor, consensus) surface together, instead of only from inside a project.',
	]}
/>
