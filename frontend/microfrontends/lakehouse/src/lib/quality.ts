import type { Producers } from '@rask/api/lineage';

// scope #6 quality gate. The validator records its dataQualityAssertions verdict on the run that produced a
// dataset (medallion stages do; a plain catalog table has none). `quality_passed=false` means the gate ran
// and blocked. We surface the latest run that recorded a verdict — null means no run has one, stated honestly.
export type QualityBadge = { passed: boolean; assertions: number } | null;

export function deriveQuality(producers: Producers | null | undefined): QualityBadge {
	const withVerdict = (producers?.producers ?? []).filter((p) => p.quality_passed != null);
	if (withVerdict.length === 0) return null;
	// Take the LATEST run's verdict. The producers feed is unordered (AGE returns rows in physical order,
	// and neither the Cypher nor the proxy sorts), so `.find`-ing the first non-null verdict could surface a
	// STALE earlier run — an older `passed` masking the current `blocked`, i.e. false assurance on a table
	// that failed its most recent gate. Sort by event_time DESC, exactly as the reference UI does.
	const latest = withVerdict.reduce((a, b) =>
		(b.event_time ?? '') > (a.event_time ?? '') ? b : a,
	);
	return {
		passed: latest.quality_passed === true,
		assertions: latest.quality_assertions?.length ?? 0,
	};
}
