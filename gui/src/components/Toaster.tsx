import { useEffect, useState } from "react";
import { X } from "@phosphor-icons/react";
import { subscribeToasts, dismissToast, type ToastItem } from "../lib/toast";

export function Toaster() {
  const [items, setItems] = useState<ToastItem[]>([]);
  useEffect(() => subscribeToasts(setItems), []);

  return (
    <div className="fixed bottom-4 right-4 z-[120] flex flex-col-reverse gap-2 pointer-events-none">
      {items.map((it) => (
        <ToastBox key={it.id} item={it} onClose={() => dismissToast(it.id)} />
      ))}
    </div>
  );
}

function ToastBox({ item, onClose }: { item: ToastItem; onClose: () => void }) {
  const [leaving, setLeaving] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setLeaving(true), Math.max(0, item.ttl - 220));
    return () => clearTimeout(t);
  }, [item.ttl]);

  const clickable = !!item.onClick;
  const handleClick = () => {
    if (!item.onClick) return;
    try { item.onClick(); } catch { /* noop */ }
    if (item.dismissOnClick !== false) onClose();
  };

  return (
    <div
      role={clickable ? "button" : undefined}
      tabIndex={clickable ? 0 : -1}
      onClick={clickable ? handleClick : undefined}
      onKeyDown={(e) => {
        if (!clickable) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          handleClick();
        }
      }}
      title={item.hoverHint}
      className={`pointer-events-auto flex items-center gap-2.5 pl-3 pr-1.5 py-2 rounded-lg border text-[12px] min-w-[180px] max-w-[320px] transition-colors ${
        leaving ? "anim-toast-out" : "anim-toast-in"
      } ${clickable ? "cursor-pointer hover:bg-(--surface)" : ""}`}
      style={{
        background: "var(--surface-2)",
        borderColor: "var(--border)",
        boxShadow: "var(--shadow-md)",
        color: "var(--t1)",
      }}>
      <span className="flex-1 truncate">{item.text}</span>
      <button
        onClick={(e) => { e.stopPropagation(); onClose(); }}
        className="icon-btn !w-5 !h-5 shrink-0"
        title="閉じる">
        <X size={10} weight="bold" />
      </button>
    </div>
  );
}
