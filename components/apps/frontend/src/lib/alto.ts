import type { AltoParse, BBox, Line } from '@rask/api';

const NS = 'http://www.loc.gov/standards/alto/ns-v4#';

/** Parse an ALTO 4.x XML string into lines with bbox + concatenated word text. */
export function parseAlto(xml: string): AltoParse {
	const doc = new DOMParser().parseFromString(xml, 'text/xml');

	const page = doc.getElementsByTagNameNS(NS, 'Page')[0];
	const pageWidth = page ? Number(page.getAttribute('WIDTH') || 0) : 0;
	const pageHeight = page ? Number(page.getAttribute('HEIGHT') || 0) : 0;
	const pageConfidence = page ? Number(page.getAttribute('PC') || 0) : 0;

	const lines: Line[] = [];
	const textLines = doc.getElementsByTagNameNS(NS, 'TextLine');
	for (let i = 0; i < textLines.length; i++) {
		const tl = textLines[i];
		const bbox: BBox = {
			x: Number(tl.getAttribute('HPOS') || 0),
			y: Number(tl.getAttribute('VPOS') || 0),
			w: Number(tl.getAttribute('WIDTH') || 0),
			h: Number(tl.getAttribute('HEIGHT') || 0),
		};

		const shape = tl.getElementsByTagNameNS(NS, 'Polygon')[0];
		let polygon: { x: number; y: number }[] | null = null;
		if (shape) {
			const pts = shape.getAttribute('POINTS') || '';
			polygon = pts
				.split(/\s+/)
				.filter(Boolean)
				.map((p) => {
					const [x, y] = p.split(',').map(Number);
					return { x, y };
				});
		}

		const strings = tl.getElementsByTagNameNS(NS, 'String');
		const texts: string[] = [];
		const wcs: number[] = [];
		for (let j = 0; j < strings.length; j++) {
			texts.push(strings[j].getAttribute('CONTENT') || '');
			const wc = Number(strings[j].getAttribute('WC') || 0);
			if (wc > 0) wcs.push(wc);
		}
		const text = texts.join(' ');
		const confidence = wcs.length ? wcs.reduce((a, b) => a + b, 0) / wcs.length : 0;

		const altoId = tl.getAttribute('ID') || '';
		lines.push({ id: i, altoId, bbox, polygon, text, confidence });
	}

	return { pageWidth, pageHeight, lines, pageConfidence };
}
