/** Bounding box in image-pixel coords. */
export interface BBox {
	x: number;
	y: number;
	w: number;
	h: number;
}

/** A single transcribed text line. */
export interface Line {
	id: number;
	/** ID attribute from the ALTO `<TextLine ID="...">` — used to match across pipeline stages and search hits. */
	altoId: string;
	bbox: BBox;
	polygon: { x: number; y: number }[] | null;
	text: string;
	confidence: number;
}

/** Result of parsing one ALTO file. */
export interface AltoParse {
	pageWidth: number;
	pageHeight: number;
	lines: Line[];
	pageConfidence: number;
}

/** Page entry returned by the backend listing. */
export interface PageEntry {
	key: string;
	hasAlto: boolean;
}
