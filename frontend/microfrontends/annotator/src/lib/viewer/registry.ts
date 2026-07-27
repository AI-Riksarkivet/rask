/**
 * Viewer registry — maps a media kind to its viewer component, so the annotator
 * route stays decoupled from any specific viewer. Adding a modality = one entry.
 */
import type { Component } from 'svelte';
import AudioViewer from './AudioViewer.svelte';
import ImageViewer from './ImageViewer.svelte';
import VideoViewer from './VideoViewer.svelte';
import type { MediaKind, ViewerProps } from './types';

const VIEWERS: Record<MediaKind, Component<ViewerProps>> = {
	image: ImageViewer,
	audio: AudioViewer,
	video: VideoViewer,
};

/** The viewer component for a media kind. */
export function viewerFor(kind: MediaKind): Component<ViewerProps> {
	return VIEWERS[kind];
}
