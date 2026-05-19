import { onDestroy } from "svelte";

export function useCopyFeedback(duration = 2000) {
  let copied = $state(false);
  let timer: ReturnType<typeof setTimeout> | undefined;

  function cleanup() {
    clearTimeout(timer);
  }

  async function copy(value: string) {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = value;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    cleanup();
    copied = true;
    timer = setTimeout(() => (copied = false), duration);
  }

  onDestroy(cleanup);

  return {
    get copied() {
      return copied;
    },
    copy,
  };
}
