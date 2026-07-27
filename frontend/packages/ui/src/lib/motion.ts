// @rask/ui attachments — reusable GSAP attachments for Svelte 5's {@attach} directive
// (skill: gsap + svelte-template-directives). Each factory returns an Attachment —
// `(node) => cleanup` — so it animates on mount and reverts on removal. Every factory
// is reduced-motion aware and SSR-safe (gsap only runs client-side here).
//
// Re-run semantics: {@attach fn(arg)} re-runs whenever `arg` changes. Drive the *continuous* ones
// (breathe/pulse) from a `$derived` primitive so Svelte's value-equality skips no-op re-runs while
// the dashboard reassigns its data objects every poll. One-shot `enter`/`stagger` take literals.
import type { Attachment } from 'svelte/attachments';
import { gsap, reduced } from './gsap';

/** Fade + rise in once on mount. */
export function enter(
	opts: { y?: number; delay?: number; duration?: number } = {},
): Attachment<HTMLElement> {
	const { y = 12, delay = 0, duration = 0.5 } = opts;
	return (node) => {
		if (reduced()) return;
		const tw = gsap.from(node, {
			y,
			autoAlpha: 0,
			duration,
			delay,
			clearProps: 'transform,opacity,visibility',
		});
		return () => tw.kill();
	};
}

/** Stagger the element's direct children in on mount (for groups that mount together). */
export function stagger(
	opts: { y?: number; each?: number; delay?: number } = {},
): Attachment<HTMLElement> {
	const { y = 10, each = 0.06, delay = 0 } = opts;
	return (node) => {
		if (reduced()) return;
		const tw = gsap.from(node.children, {
			y,
			autoAlpha: 0,
			duration: 0.5,
			delay,
			stagger: each,
			clearProps: 'transform,opacity,visibility',
		});
		return () => tw.kill();
	};
}

/** Animate a progress-fill width to `pct` (0–100). Re-runs when pct changes. */
export function bar(pct: number): Attachment<HTMLElement> {
	return (node) => {
		if (reduced()) {
			node.style.width = `${pct}%`;
			return;
		}
		const tw = gsap.to(node, {
			width: `${pct}%`,
			duration: 0.55,
			ease: 'power2.out',
			overwrite: 'auto',
		});
		return () => tw.kill();
	};
}

/** Breathe opacity while `active` (e.g. a running progress bar). */
export function breathe(active: boolean): Attachment<HTMLElement> {
	return (node) => {
		if (!active || reduced()) {
			gsap.set(node, { opacity: 1 });
			return;
		}
		const tw = gsap.to(node, {
			opacity: 0.45,
			duration: 0.75,
			repeat: -1,
			yoyo: true,
			ease: 'sine.inOut',
		});
		return () => {
			tw.kill();
			gsap.set(node, { opacity: 1 });
		};
	};
}

/** Radar-ping ring while `active` (e.g. a running node). `rgb` is a bare "r, g, b" triple. */
export function pulse(active: boolean, rgb = '255, 193, 77'): Attachment<HTMLElement> {
	return (node) => {
		if (!active || reduced()) {
			gsap.set(node, { clearProps: 'boxShadow' });
			return;
		}
		const tw = gsap.fromTo(
			node,
			{ boxShadow: `0 0 0 0 rgba(${rgb}, 0.5)` },
			{ boxShadow: `0 0 0 8px rgba(${rgb}, 0)`, duration: 1.2, repeat: -1, ease: 'sine.out' },
		);
		return () => {
			tw.kill();
			gsap.set(node, { clearProps: 'boxShadow' });
		};
	};
}

/** Scale-pop on mount and whenever `key` changes (state-change feedback). */
export function pop(key: unknown): Attachment<HTMLElement> {
	void key; // read by the caller's {@attach pop(state)} so the pop re-fires on change
	return (node) => {
		if (reduced()) return;
		const tw = gsap.fromTo(
			node,
			{ scale: 0.94 },
			{ scale: 1, duration: 0.4, ease: 'back.out(1.8)', clearProps: 'scale' },
		);
		return () => tw.kill();
	};
}

/** Tween a numeric label from its current value to `value`. Owns the node's textContent. */
export function countUp(value: number): Attachment<HTMLElement> {
	return (node) => {
		if (reduced()) {
			node.textContent = String(value);
			return;
		}
		const state = { n: Number(node.textContent?.replace(/[^\d.-]/g, '')) || 0 };
		const tw = gsap.to(state, {
			n: value,
			duration: 0.6,
			ease: 'power2.out',
			onUpdate: () => {
				node.textContent = String(Math.round(state.n));
			},
		});
		return () => tw.kill();
	};
}
