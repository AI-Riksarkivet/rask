/**
 * The TEMPLATE GALLERY — full ontologies for the labeling tasks the workspace already composes
 * (finding 8: the annotator is a general multimodal tool, and templates are its organizing
 * mechanism, the way Label Studio's labeling configs are).
 *
 * A template is a COMPLETE `LabelOntology`: per-class tools, typed attributes, relations,
 * `allow_empty` — everything the create dialog's free-form path cannot author (it applies one
 * uniform tool list to every typed name). Picking one seeds the whole contract; the enforceable
 * document is still the ontology itself, validated server-side at create and enforced at submit.
 *
 * Every workspace surface here is driven by the ontology alone, so each template IS a layout
 * choice too: `tag` classes grow the classification bar, `text` classes the span surfaces,
 * relations the link rail, attributes the inspector's editor — no template-specific code.
 */

export interface TemplateAttr {
	name: string;
	type: 'free' | 'int' | 'enum' | 'bool';
	choices?: string[];
	required?: boolean;
}

export interface TemplateClass {
	name: string;
	tools: string[];
	attributes?: TemplateAttr[];
	required?: boolean;
}

export interface ProjectTemplate {
	id: string;
	name: string;
	description: string;
	ontology: {
		kind: string;
		classes: TemplateClass[];
		relations?: {
			name: string;
			from_classes: string[];
			to_classes: string[];
			required?: boolean;
		}[];
		allow_empty?: boolean;
	};
}

const READING_ORDER: TemplateAttr = { name: 'order', type: 'int' };

export const PROJECT_TEMPLATES: readonly ProjectTemplate[] = [
	{
		id: 'ocr-layout',
		name: 'OCR / HTR layout + transcription',
		description:
			'Layout regions with per-class tools, transcription per region, entity spans into the text, reading order, and page-level condition tags — the composite document task.',
		ontology: {
			kind: 'ocr-layout',
			classes: [
				{ name: 'header', tools: ['bbox'], attributes: [READING_ORDER] },
				{
					name: 'paragraph',
					tools: ['polygon', 'bbox'],
					required: true,
					attributes: [
						READING_ORDER,
						{ name: 'script', type: 'enum', choices: ['blackletter', 'cursive', 'print'] },
						{ name: 'crossed-out', type: 'bool' },
					],
				},
				{ name: 'marginalia', tools: ['polygon', 'bbox'], attributes: [READING_ORDER] },
				{ name: 'page-number', tools: ['bbox'], attributes: [READING_ORDER] },
				{ name: 'person', tools: ['text'] },
				{ name: 'place', tools: ['text'] },
				{ name: 'damaged', tools: ['tag'] },
				{ name: 'faded-ink', tools: ['tag'] },
			],
			relations: [{ name: 'annotates', from_classes: ['marginalia'], to_classes: ['paragraph'] }],
		},
	},
	{
		id: 'object-detection',
		name: 'Object detection',
		description: 'Boxes with a closed class set — the plain detection task.',
		ontology: {
			kind: 'object-detection',
			classes: [
				{ name: 'stamp', tools: ['bbox'] },
				{ name: 'signature', tools: ['bbox'] },
				{ name: 'seal', tools: ['bbox'] },
			],
		},
	},
	{
		id: 'image-segmentation',
		name: 'Segmentation',
		description: 'Polygon/mask instances; the brush and SAM click-to-segment both apply.',
		ontology: {
			kind: 'image-segmentation',
			classes: [
				{ name: 'region', tools: ['polygon', 'mask'] },
				{ name: 'illustration', tools: ['polygon', 'mask'] },
			],
		},
	},
	{
		id: 'image-classification',
		name: 'Classification (whole item)',
		description:
			'Item-level choices as the chip bar over any modality — multilabel; the Choices question.',
		ontology: {
			kind: 'image-classification',
			classes: [
				{ name: 'letter', tools: ['tag'] },
				{ name: 'form', tools: ['tag'] },
				{ name: 'map', tools: ['tag'] },
				{ name: 'photograph', tools: ['tag'] },
			],
			allow_empty: true,
		},
	},
	{
		id: 'token-classification',
		name: 'NER / token classification',
		description:
			'Entity spans into the document text — the doccano interaction, digit hotkeys included.',
		ontology: {
			kind: 'token-classification',
			classes: [
				{ name: 'person', tools: ['text'] },
				{ name: 'place', tools: ['text'] },
				{ name: 'date', tools: ['text'] },
				{ name: 'organisation', tools: ['text'] },
			],
		},
	},
	{
		id: 'document-question-answering',
		name: 'DocQA / key information',
		description: 'Question and value regions linked by a typed relation — KIE and document VQA.',
		ontology: {
			kind: 'document-question-answering',
			classes: [
				{ name: 'question', tools: ['bbox', 'text'] },
				{ name: 'value', tools: ['bbox', 'text'] },
			],
			relations: [
				{ name: 'answers', from_classes: ['question'], to_classes: ['value'], required: true },
			],
		},
	},
	{
		id: 'comparison',
		name: 'Comparison / preference',
		description:
			'Pick between candidates of a multi-key item in the side-by-side compare view — the pairwise question.',
		ontology: {
			kind: 'comparison',
			classes: [
				{ name: 'preferred', tools: ['tag'] },
				{ name: 'unusable', tools: ['tag'] },
			],
			allow_empty: true,
		},
	},
	{
		id: 'reading-order',
		name: 'Reading order',
		description:
			'Line regions carrying a required order — the lane reads in declared order, not geometry.',
		ontology: {
			kind: 'reading-order',
			classes: [
				{ name: 'line', tools: ['bbox'], attributes: [{ ...READING_ORDER, required: true }] },
			],
		},
	},
];

export const templateById = (id: string): ProjectTemplate | undefined =>
	PROJECT_TEMPLATES.find((t) => t.id === id);
