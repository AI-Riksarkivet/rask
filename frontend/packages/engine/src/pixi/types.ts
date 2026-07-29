import type { Application } from 'pixi.js';
import type { ArrowDataPlugin } from './ArrowDataPlugin.js';
import type { ImagePlugin } from './ImagePlugin.js';
import type { InteractionManager } from '../interaction/InteractionManager.js';

export interface PixiContext {
	app: Application;
	plugins: {
		image: ImagePlugin;
		arrow: ArrowDataPlugin;
		interaction: InteractionManager;
	};
}

export interface ViewportBounds {
	x: number;
	y: number;
	width: number;
	height: number;
}

export type Tool =
	| 'select'
	| 'pan'
	| 'rect'
	| 'polygon'
	| 'magnetic'
	| 'lasso'
	| 'pencil'
	| 'point'
	| 'line'
	| 'brush';
