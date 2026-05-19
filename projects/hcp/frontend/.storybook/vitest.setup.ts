import { setProjectAnnotations } from "@storybook/sveltekit";
import { afterAll } from "vitest";
import * as previewAnnotations from "./preview";

const annotations = setProjectAnnotations([previewAnnotations]);

afterAll(() => {
  if (typeof annotations?.cleanup === "function") {
    annotations.cleanup();
  }
});
