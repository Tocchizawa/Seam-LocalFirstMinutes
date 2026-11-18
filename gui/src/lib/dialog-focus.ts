import { useLayoutEffect, useRef } from "react";

const focusable = "button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex^='-'])";

export function useDialogFocus(open = true) {
  const dialog = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const node = dialog.current;
    if (!open || !node) return;
    const previous = document.activeElement;
    const controls = () => Array.from(node.querySelectorAll<HTMLElement>(focusable)).filter((element) =>
      element.tabIndex >= 0 && element.getClientRects().length > 0,
    );
    const target = node.querySelector<HTMLElement>("[data-initial-focus]") ?? controls()[0] ?? node;
    if (!node.contains(document.activeElement)) target.focus({ preventScroll: true });

    const handleTab = (event: KeyboardEvent) => {
      if (event.key !== "Tab" || event.defaultPrevented || !node.contains(document.activeElement)) return;
      const items = controls();
      const first = items[0];
      const last = items[items.length - 1];
      if (!first || !last) {
        event.preventDefault();
      } else if (event.shiftKey && (document.activeElement === first || document.activeElement === node)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    node.addEventListener("keydown", handleTab);
    return () => {
      node.removeEventListener("keydown", handleTab);
      if (previous instanceof HTMLElement) {
        requestAnimationFrame(() => {
          if (previous.isConnected && !previous.closest("[inert]") && document.activeElement === document.body) {
            previous.focus({ preventScroll: true });
          }
        });
      }
    };
  }, [open]);

  return dialog;
}
