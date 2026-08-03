// Finale drive: the WHOLE funnel against the live cluster (Dapr actors, OpenFGA, catalog).
//  A. media (:5173): real search → "Send to project…" → create-and-send (screenshot).
//  B. annotator (:5177): review-waived project + a REAL corpus key → claim → open the canvas →
//     DRAW a rectangle → Save → the task DRAFT carries the shape (S10 first half, live) →
//     submit → freeze → publish → published, and the table's rows carry real shapes.
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const MEDIA = 'http://localhost:5173';
const ANNO = 'http://localhost:5177';
const SHOTS = process.env.SHOTS_DIR ?? '/tmp/anno-shots2';
mkdirSync(SHOTS, { recursive: true });

const j = async (res) => {
	if (!res.ok()) throw new Error(`${res.url()} -> ${res.status()}: ${await res.text()}`);
	return res.json();
};

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const api = page.request;
const ts = Date.now().toString(36);

// ── discover a REAL corpus key through the viewer (the cluster corpus, not a fixture) ────────────
const docs = await j(await api.get(`${ANNO}/annotator/api/documents?page=1&page_size=1`));
const doc = docs.docs[0];
if (!doc) throw new Error('cluster corpus has no documents');
const transcript = await j(
	await api.get(`${ANNO}/annotator/api/doc-transcript/${doc.doc_id}?page_size=1`),
);
const chunk = transcript.chunks[0];
const KEY = `${doc.doc_id}/${chunk.speech_id}/${chunk.chunk_id}`;
console.log('corpus key:', KEY);

// ── A · media: search → send-to-project ──────────────────────────────────────────────────────────
await page.goto(`${MEDIA}/media/`);
const input = page.getByPlaceholder(/Search/i).first();
await input.waitFor({ timeout: 30_000 });
let sent = false;
for (const q of ['och', 'att', 'som', 'the']) {
	await input.fill(q);
	await input.press('Enter');
	try {
		await page.getByRole('button', { name: 'Send to project…' }).first().waitFor({ timeout: 8000 });
		sent = true;
		break;
	} catch {
		console.log(`no hits for '${q}' — trying the next query`);
	}
}
if (!sent) throw new Error('no search query produced hits with a send button');
await page.getByRole('button', { name: 'Send to project…' }).first().click();
await page.getByPlaceholder('slug (vasa-portraits)').fill(`funnel-${ts}`);
await page.getByRole('button', { name: /Create & send/ }).click();
await page.getByTestId('send-done').waitFor({ timeout: 15_000 });
await page.screenshot({ path: `${SHOTS}/media-send.png` });
console.log('A · media send: done (screenshot)');

// ── B · annotator: the canvas → draft → publish leg ──────────────────────────────────────────────
const AAPI = `${ANNO}/annotator/api`;
const project = await j(
	await api.post(`${AAPI}/projects`, {
		data: {
			tenant: 'default',
			slug: `canvas-${ts}`,
			title: 'Canvas leg',
			review_required: false,
			label_schema: { classes: [{ name: 'portrait', shape_types: ['bbox'] }] },
		},
	}),
);
await j(
	await api.post(`${AAPI}/projects/${project.project_id}/events`, { data: { event: 'open' } }),
);
await j(
	await api.post(`${AAPI}/projects/${project.project_id}/items`, {
		data: { items: [{ source: { kind: 'chunks', keys: [KEY] }, media: { kind: 'image' } }] },
	}),
);
const listing = await j(await api.get(`${AAPI}/projects/${project.project_id}/tasks`));
const taskId = Object.keys(listing.tasks)[0];
await j(await api.post(`${AAPI}/tasks/${taskId}/events`, { data: { event: 'claim' } }));
console.log('B · project + claimed task:', project.project_id, taskId);

// The task-opened canvas (the queue's Annotate href shape).
await page.goto(`${ANNO}/annotator/?keys=${encodeURIComponent(KEY)}&task=${taskId}`);
await page.getByTitle('Redo (Ctrl+Shift+Z)').waitFor({ timeout: 30_000 });
// Enter edit mode if we are in view mode; pick the Rectangle tool; drag a box on the canvas.
const modeToggle = page.getByTitle('View mode (click to edit)');
if (await modeToggle.isVisible().catch(() => false)) await modeToggle.click();
await page.getByTitle(/^Rectangle \(3\)/).click();
const canvas = page.locator('canvas').first();
const box = await canvas.boundingBox();
if (!box) throw new Error('no canvas to draw on');
const cx = box.x + box.width / 2;
const cy = box.y + box.height / 2;
await page.mouse.move(cx - 60, cy - 40);
await page.mouse.down();
await page.mouse.move(cx + 60, cy + 40, { steps: 8 });
await page.mouse.up();
await page.getByTitle('Unsaved edits').waitFor({ timeout: 10_000 });
await page.screenshot({ path: `${SHOTS}/canvas-drawn.png` });
await page.getByTitle('Save to Lance (Ctrl+S)').click();
await page.getByText('Annotations saved').waitFor({ timeout: 20_000 });
console.log('B · drew + saved on the canvas');

// The S10 sync, live: the task DRAFT now carries the drawn shape.
const draft = await j(await api.get(`${AAPI}/tasks/${taskId}/draft`));
if (!draft.shapes?.length)
	throw new Error('the task draft is EMPTY — the canvas sync did not land');
console.log(`B · task draft carries ${draft.shapes.length} shape(s):`, draft.shapes[0].shape_type);

// submit (review waived → accepted) → freeze → publish → published.
await j(await api.post(`${AAPI}/tasks/${taskId}/events`, { data: { event: 'submit' } }));
await j(
	await api.post(`${AAPI}/projects/${project.project_id}/events`, { data: { event: 'freeze' } }),
);
await j(
	await api.post(`${AAPI}/projects/${project.project_id}/events`, { data: { event: 'publish' } }),
);
let final = null;
for (let i = 0; i < 40; i += 1) {
	const detail = await j(await api.get(`${AAPI}/projects/${project.project_id}`));
	if (['published', 'publish_failed'].includes(detail.project.state)) {
		final = detail.project;
		break;
	}
	await page.waitForTimeout(2000);
}
if (!final) throw new Error('publish never reached a terminal state');
console.log(
	'B · publish terminal:',
	final.state,
	JSON.stringify(final.published ?? final.publish_error),
);
await page.goto(`${ANNO}/annotator/projects/${project.project_id}`);
await page
	.getByText(/published|publish failed/)
	.first()
	.waitFor({ timeout: 15_000 });
await page.waitForTimeout(400);
await page.screenshot({ path: `${SHOTS}/publish-final2.png` });
if (final.state !== 'published') throw new Error(`publish FAILED: ${final.publish_error}`);
console.log('DRIVE2 COMPLETE — shapes travelled from the canvas into', final.published.table_id);

await browser.close();
