/**
 * CSF3 interaction tests for StepProgress.
 *
 * Tests status transitions and error states using a test harness
 * with buttons to trigger step progression.
 */
import type { Meta, StoryObj } from "@storybook/svelte";
import { expect, within } from "storybook/test";
import TestHarness from "./step-progress-test-harness.svelte";

const meta = {
  title: "Tests/StepProgress Interactions",
  // deno-lint-ignore no-explicit-any -- Storybook Meta types incompatible with Svelte 5 Component
  component: TestHarness as any,
  tags: ["!autodocs"],
} satisfies Meta<typeof TestHarness>;

export default meta;
type Story = StoryObj<typeof meta>;

/**
 * Verify initial state renders all steps as pending.
 */
export const RendersInitialState: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);

    await expect(
      canvas.getByText("Validate configuration"),
    ).toBeInTheDocument();
    await expect(canvas.getByText("Create namespace")).toBeInTheDocument();
    await expect(
      canvas.getByText("Apply permissions"),
    ).toBeInTheDocument();

    // All pending — no error badges visible
    const errorBadges = canvasElement.querySelectorAll(
      '[data-slot="badge"][class*="destructive"]',
    );
    await expect(errorBadges.length).toBe(0);
  },
};

/**
 * Run steps and verify they complete successfully.
 */
export const CompletesAllSteps: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);

    const runBtn = canvas.getByTestId("run-btn");
    runBtn.click();

    // Wait for all steps to finish (1200ms + buffer)
    await new Promise((r) => setTimeout(r, 1600));

    // All steps should show checkmarks (done state)
    const checkIcons = canvasElement.querySelectorAll(
      'svg[class*="text-green"]',
    );
    await expect(checkIcons.length).toBe(3);
  },
};

/**
 * Run with error and verify the failed step shows error badge.
 */
export const ShowsErrorState: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);

    const errorBtn = canvas.getByTestId("error-btn");
    errorBtn.click();

    // Wait for error to occur (800ms + buffer)
    await new Promise((r) => setTimeout(r, 1200));

    // Should show "Quota exceeded" error badge
    await expect(canvas.getByText("Quota exceeded")).toBeInTheDocument();

    // First step should be done (green), second failed (red)
    const checkIcons = canvasElement.querySelectorAll(
      'svg[class*="text-green"]',
    );
    await expect(checkIcons.length).toBe(1);

    const errorIcons = canvasElement.querySelectorAll(
      'svg[class*="text-destructive"]',
    );
    await expect(errorIcons.length).toBe(1);
  },
};
