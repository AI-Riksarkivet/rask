import type { Locator, Page } from '@playwright/test';

/**
 * The cell of `row` under the column headed `header`, in the table that has that column: found by the
 * header's name, so a spec does not break, or read the wrong cell, when a column is added or moved.
 */
export async function cellUnder(page: Page, row: string, header: string): Promise<Locator> {
	const table = page
		.getByRole('table')
		.filter({ has: page.getByRole('columnheader', { name: header, exact: true }) });
	await table.waitFor();
	const headers = (await table.getByRole('columnheader').allInnerTexts()).map((h) =>
		h.trim().toLowerCase(),
	);
	const column = headers.indexOf(header.toLowerCase());
	if (column < 0) throw new Error(`no column headed "${header}": ${headers.join(', ')}`);
	// `tr`, not the row role: a list whose rows open a drawer gives each row the button role.
	return table.locator('tr').filter({ hasText: row }).getByRole('cell').nth(column);
}
