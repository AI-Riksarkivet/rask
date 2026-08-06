<script lang="ts">
	// LLM-as-a-judge — planned, not built. See `PlannedArea` for why a stub says so loudly.
	//
	// The material it will stand on already half-exists: a project declares `consensus_n`, and
	// `bulk-events.ts` derives the review vocabulary (`approve` / `request_changes`) from the task
	// actor's own `legal_events`. What is missing is the JUDGE — today agreement is decided by
	// people, and #55 records that even their agreement is scored as an exact label-multiset match
	// with no IoU and no kappa. A model judge lands on top of that scorer, so it cannot be built
	// before the scorer is worth trusting.
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
