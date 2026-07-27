/**
 * Reactive app color mode — tracks the `.dark` class on `<html>` (toggled by the estate's
 * theme button / mode-watcher) so consumers update LIVE instead of reading it once at mount.
 *
 * Call once at component init (it registers a `$effect` that observes the class) and read
 * `.current` / `.isDark`. `current` is `'dark' | 'light'` — a plain string, not a library
 * type — so anything can consume it: the Svelte Flow canvas (`colorMode`), a WebGPU atlas,
 * a canvas chart. It deliberately does NOT import `$app/environment`, so it stays usable
 * from this framework-agnostic package (SSR-safe via a `document` guard).
 */
export function useColorMode(): { readonly current: 'dark' | 'light'; readonly isDark: boolean } {
	let isDark = $state(
		typeof document !== 'undefined' && document.documentElement.classList.contains('dark'),
	);

	$effect(() => {
		const html = document.documentElement;
		const sync = () => (isDark = html.classList.contains('dark'));
		const observer = new MutationObserver(sync);
		observer.observe(html, { attributes: true, attributeFilter: ['class'] });
		sync(); // catch any change between the initial read and the effect mounting
		return () => observer.disconnect();
	});

	return {
		get current() {
			return isDark ? 'dark' : 'light';
		},
		get isDark() {
			return isDark;
		},
	};
}
