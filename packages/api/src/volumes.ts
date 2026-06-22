import type { PageEntry } from './types';

// @rask/api/volumes — S3/IIIF image + ALTO + page listing.
export async function listPages(
	volume: string,
	fetchFn: typeof fetch = fetch,
): Promise<PageEntry[]> {
	// `fetchFn` lets SSR `load` functions pass SvelteKit's provided `fetch`, which
	// resolves the relative `/api/...` URL against the request origin (a bare global
	// fetch has no origin on the server). Client callers use the default global fetch.
	const res = await fetchFn(`/api/volumes/${encodeURIComponent(volume)}/pages`);
	if (!res.ok) throw new Error(`listPages(${volume}): HTTP ${res.status}`);
	return res.json();
}

export function imageUrl(volume: string, key: string): string {
	return `/api/volumes/${encodeURIComponent(volume)}/pages/${encodeURIComponent(key)}/image`;
}

export function altoUrl(volume: string, key: string): string {
	return `/api/volumes/${encodeURIComponent(volume)}/pages/${encodeURIComponent(key)}/alto`;
}

export async function fetchAlto(volume: string, key: string): Promise<string | null> {
	const res = await fetch(altoUrl(volume, key));
	if (res.status === 404) return null;
	if (!res.ok) throw new Error(`fetchAlto: HTTP ${res.status}`);
	return res.text();
}
