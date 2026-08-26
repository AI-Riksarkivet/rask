<!--
	Draw the route ELK computed, instead of guessing one.

	`smoothstep` — Svelte Flow's default orthogonal edge — knows two endpoints and nothing else. It
	cannot know that a node sits between them, so a long edge spanning several layers is drawn
	straight across whatever it crosses. ELK's layered pass routes those edges through reserved
	slots and hands back the corners it turned; discarding them and re-deriving a shape from the
	endpoints throws away the one phase the layout engine was added for.

	Marquez draws the same geometry the same way (`Edge/ElbowEdge.tsx` renders ELK's bend points as
	an SVG polyline), so this is convergence rather than invention. The corner rounding is the one
	deviation: a hard right angle at every bend reads as a schematic, and rounding it costs a
	quadratic per corner.

	THE ROUTE GOES STALE THE MOMENT A NODE MOVES, which Marquez never has to handle because its
	canvas does not drag. Here a dragged node keeps its edges' old elbows, which would be visibly
	wrong — an edge turning a corner around a node that has left. So the route is used only while the
	endpoints still sit where ELK put them; past a small drift the edge falls back to `smoothstep`,
	which is endpoint-derived and therefore always right about its own ends. The effect is that
	dragging a node straightens its edges, and the next layout restores the routed ones.
-->
<script lang="ts">
	import { BaseEdge, getSmoothStepPath, type EdgeProps } from '@xyflow/svelte';

	import type { ElkRoute } from './elk-layout';

	let {
		id,
		sourceX,
		sourceY,
		targetX,
		targetY,
		sourcePosition,
		targetPosition,
		markerEnd,
		style,
		label,
		labelStyle,
		data,
	}: EdgeProps = $props();

	/**
	 * How far an endpoint may drift from ELK's before the route is abandoned, in px.
	 *
	 * Not zero: the post-layout collision pass nudges cards by a few px, and handles sit on a node's
	 * edge rather than at the corner ELK reported, so an exact match never happens. Wide enough to
	 * survive both, narrow enough that an actual drag — which moves a node by tens of px at least —
	 * always falls out.
	 */
	const DRIFT = 24;

	/** Corner radius at each bend. Clamped per-corner to half the shorter adjoining leg, so a tight
	 *  zig-zag rounds less rather than overshooting into the next segment. */
	const RADIUS = 8;

	const route = $derived((data as { route?: ElkRoute } | undefined)?.route ?? null);

	/** True while ELK's route still describes THIS edge's current endpoints. */
	const routeIsCurrent = $derived(
		route !== null &&
			route.bendPoints.length > 0 &&
			Math.abs(route.start.x - sourceX) + Math.abs(route.start.y - sourceY) <= DRIFT &&
			Math.abs(route.end.x - targetX) + Math.abs(route.end.y - targetY) <= DRIFT,
	);

	/**
	 * A rounded polyline through `points`.
	 *
	 * Each interior point becomes a quadratic whose control point IS the corner, entered and left
	 * along the two legs — the standard way to round a polyline without moving the path off it.
	 */
	function roundedPath(points: { x: number; y: number }[]): string {
		const first = points[0];
		const last = points[points.length - 1];
		if (!first || !last) return '';
		let d = `M${first.x},${first.y}`;
		for (let i = 1; i < points.length - 1; i += 1) {
			const prev = points[i - 1];
			const corner = points[i];
			const next = points[i + 1];
			if (!prev || !corner || !next) continue;
			const inLen = Math.hypot(corner.x - prev.x, corner.y - prev.y);
			const outLen = Math.hypot(next.x - corner.x, next.y - corner.y);
			// A degenerate leg (two identical points) would divide by zero and emit NaN into the path,
			// which SVG renders as nothing at all — one silently missing edge.
			if (inLen === 0 || outLen === 0) {
				d += ` L${corner.x},${corner.y}`;
				continue;
			}
			const r = Math.min(RADIUS, inLen / 2, outLen / 2);
			const enter = {
				x: corner.x - ((corner.x - prev.x) / inLen) * r,
				y: corner.y - ((corner.y - prev.y) / inLen) * r,
			};
			const leave = {
				x: corner.x + ((next.x - corner.x) / outLen) * r,
				y: corner.y + ((next.y - corner.y) / outLen) * r,
			};
			d += ` L${enter.x},${enter.y} Q${corner.x},${corner.y} ${leave.x},${leave.y}`;
		}
		return `${d} L${last.x},${last.y}`;
	}

	/**
	 * The path AND where a label sits on it.
	 *
	 * Both come from the same branch on purpose: `getSmoothStepPath` returns its own label anchor,
	 * and a routed path needs one derived from the route. Computing the path in one place and the
	 * anchor in another is how a label ends up floating beside an edge it does not belong to — which
	 * matters here because the column graph labels every edge with its transformation.
	 */
	/** The point half way along a polyline, measured by length. */
	function pointAtHalfLength(points: { x: number; y: number }[]): { x: number; y: number } {
		const first = points[0] ?? { x: 0, y: 0 };
		let total = 0;
		for (let i = 1; i < points.length; i += 1) {
			const a = points[i - 1];
			const b = points[i];
			if (a && b) total += Math.hypot(b.x - a.x, b.y - a.y);
		}
		let travelled = 0;
		const half = total / 2;
		for (let i = 1; i < points.length; i += 1) {
			const a = points[i - 1];
			const b = points[i];
			if (!a || !b) continue;
			const seg = Math.hypot(b.x - a.x, b.y - a.y);
			if (travelled + seg >= half) {
				// `seg` is non-zero here: a zero-length segment cannot carry `travelled` past `half`
				// unless `half` is already 0, which the degenerate guard below covers.
				const t = seg === 0 ? 0 : (half - travelled) / seg;
				return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t };
			}
			travelled += seg;
		}
		return first;
	}

	const geometry = $derived.by((): { path: string; labelX: number; labelY: number } => {
		if (routeIsCurrent && route) {
			// ANCHORED TO THE LIVE HANDLES, not to ELK's start/end: Svelte Flow positions handles on
			// the node's edge and ELK reports its own port coordinate, so using ELK's endpoints leaves
			// a visible gap between the arrow and the card. The bends in between are ELK's.
			const points = [{ x: sourceX, y: sourceY }, ...route.bendPoints, { x: targetX, y: targetY }];
			// HALFWAY ALONG THE PATH BY LENGTH, not the middle vertex and not the midpoint of the
			// straight line between the ends.
			//
			// The straight-line midpoint can land on top of the very node the route was steered
			// around. The middle vertex is worse in the common case: orthogonal routes share vertical
			// channels, so every edge converging on one node puts its label at almost the same point —
			// measured on the column graph, pairs of `IDENTITY` labels stacked on each other and read
			// as one smudge. Arc length spreads them, because two edges into the same node still
			// differ in where their halfway point falls.
			const mid = pointAtHalfLength(points);
			return { path: roundedPath(points), labelX: mid.x, labelY: mid.y };
		}
		const [smooth, labelX, labelY] = getSmoothStepPath({
			sourceX,
			sourceY,
			targetX,
			targetY,
			sourcePosition,
			targetPosition,
		});
		return { path: smooth, labelX, labelY };
	});
</script>

<BaseEdge
	{id}
	path={geometry.path}
	labelX={geometry.labelX}
	labelY={geometry.labelY}
	{label}
	{labelStyle}
	{markerEnd}
	{style}
/>
