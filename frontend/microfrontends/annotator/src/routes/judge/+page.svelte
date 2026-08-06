<script lang="ts">
	// LLM-as-a-judge — planned, not built. See `PlannedArea` for why a stub says so loudly.
	//
	// The material it will stand on already exists: a project declares `consensus_n`,
	// `bulk-events.ts` derives the review vocabulary (`approve` / `request_changes`) from the task
	// actor's own `legal_events`, and the SCORER is now worth standing on — #55 replaced the
	// exact-label-multiset comparison with geometry-aware, chance-corrected agreement (shapes
	// matched at IoU >= 0.5, labels scored with Fleiss' kappa). That was the stated blocker: a model
	// judge is measured BY that scorer, so a scorer which called two boxes in different places a
	// perfect agreement would have made a judge's reported quality meaningless.
	//
	// What is still missing is the JUDGE itself — running a model over submitted tasks and recording
	// its verdict beside the human one.
	import PlannedArea from '$lib/nav/PlannedArea.svelte';
</script>

<PlannedArea
	title="LLM-as-a-judge"
	summary="Score submitted annotations with a model instead of a second human, and route only the
	         disagreements to review."
	intent={[
	'Run a judge over submitted tasks and record a verdict beside the human one, rather than in place of it.',
	'Send only the items the judge and the annotator disagree on to human review — which is what makes a consensus_n of 2 affordable at corpus scale.',
	'Report judge-vs-human agreement over time, so a judge that drifts is visible before it has silently graded a batch.',
	'Reuse the review vocabulary the task actor already declares (approve / request_changes), so a judge verdict is the same kind of event as a reviewer verdict.',
]}
/>
