/**
 * Nudge overlapping nodes apart, against the boxes actually RENDERED.
 *
 * Adapted from Svelte Flow's own `layout/node-collisions` example
 * (https://svelteflow.dev/examples/layout/node-collisions) — same algorithm, typed to this package's
 * needs and given the reasoning below.
 *
 * **Why a layout engine is not enough.** ELK reserves exactly the box it is TOLD. A card whose height
 * is content-driven — the medallion cards wrap version/failure/tag chips onto further rows — is not
 * knowable before it renders, so the first layout necessarily runs on an estimate. Measured on the
 * deployed estate: cards render 51–129px tall against a declared 64, 82 of 85 exceed the declaration,
 * and 22 node pairs genuinely overlapped, one of them by 200×31px.
 *
 * Svelte Flow writes `measured` after it renders a node, so this pass is the only place the REAL
 * geometry exists. Feeding measurements back into ELK closes the loop eventually, but not on first
 * paint — nothing re-triggers a layout when a measurement arrives — which is why this exists as well
 * as, not instead of, the `size` hook.
 *
 * **It only separates; it never re-flows.** Each overlapping pair is pushed apart along its SMALLEST
 * overlap axis by half the overlap each, so the layered structure ELK computed survives — a node moves
 * by the minimum needed to stop colliding, not to a new column. Nodes that do not overlap are returned
 * by reference, so a caller can tell what moved.
 */
import type { Node } from '@xyflow/svelte';

export interface ResolveCollisionsOptions {
	/** Cap on relaxation passes. Each pass separates every colliding pair once; a handful converges. */
	maxIterations?: number;
	/** Overlap in px below which a pair is left alone — sub-pixel touching is not a collision. */
	overlapThreshold?: number;
	/** Breathing room added around every box before testing, so cards do not end up flush. */
	margin?: number;
}

interface Box<T extends Node> {
	x: number;
	y: number;
	width: number;
	height: number;
	moved: boolean;
	node: T;
}

/**
 * `measured` is what Svelte Flow wrote after rendering; the explicit `width`/`height` are what a
 * caller declared. Prefer the MEASURED value — the declared one is the estimate this pass exists to
 * correct — and fall back to a card-sized default rather than 0, since a zero box collides with
 * nothing and would make the node invisible to the algorithm.
 */
function boxOf<T extends Node>(node: T, margin: number): Box<T> {
	const width = node.measured?.width ?? node.width ?? 200;
	const height = node.measured?.height ?? node.height ?? 64;
	return {
		x: node.position.x - margin,
		y: node.position.y - margin,
		width: width + margin * 2,
		height: height + margin * 2,
		node,
		moved: false,
	};
}

/**
 * GENERIC on the node type: this only ever rewrites `position`, so a caller holding a discriminated
 * union of custom nodes gets that same union back. Returning bare `Node[]` would force every caller
 * to re-assert its own type, which is exactly where a real type error would get cast away.
 */
export function resolveCollisions<T extends Node>(
	nodes: T[],
	{ maxIterations = 50, overlapThreshold = 0.5, margin = 0 }: ResolveCollisionsOptions = {},
): T[] {
	const boxes = nodes.map((n) => boxOf(n, margin));

	for (let iter = 0; iter <= maxIterations; iter += 1) {
		let moved = false;

		for (let i = 0; i < boxes.length; i += 1) {
			for (let j = i + 1; j < boxes.length; j += 1) {
				const a = boxes[i];
				const b = boxes[j];
				if (!a || !b) continue;

				const dx = a.x + a.width * 0.5 - (b.x + b.width * 0.5);
				const dy = a.y + a.height * 0.5 - (b.y + b.height * 0.5);
				// Penetration depth on each axis: positive means the projections overlap.
				const px = (a.width + b.width) * 0.5 - Math.abs(dx);
				const py = (a.height + b.height) * 0.5 - Math.abs(dy);

				if (px > overlapThreshold && py > overlapThreshold) {
					a.moved = true;
					b.moved = true;
					moved = true;
					// Separate along the SMALLEST overlap axis — the shortest move that resolves it,
					// which is what keeps ELK's column structure intact.
					if (px < py) {
						const shift = (px / 2) * (dx > 0 ? 1 : -1);
						a.x += shift;
						b.x -= shift;
					} else {
						const shift = (py / 2) * (dy > 0 ? 1 : -1);
						a.y += shift;
						b.y -= shift;
					}
				}
			}
		}

		if (!moved) break;
	}

	return boxes.map((box) =>
		box.moved ? { ...box.node, position: { x: box.x + margin, y: box.y + margin } } : box.node,
	);
}
