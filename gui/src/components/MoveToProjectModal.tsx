import { useEffect, useState } from "react";
import { X, FolderOpen, Check } from "@phosphor-icons/react";
import type { Project } from "../lib/api";

interface Props {
  minutesTitle: string;
  currentProjectId: string;
  projects: Project[];
  onClose: () => void;
  onPick: (targetProjectId: string, targetName: string) => Promise<void> | void;
}

export function MoveToProjectModal({
  minutesTitle, currentProjectId, projects, onClose, onPick,
}: Props) {
  const [closing, setClosing] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const others = projects.filter((p) => p.id !== currentProjectId);

  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === "Escape") handleClose(); };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleClose = () => {
    if (saving) return;
    setClosing(true);
    setTimeout(onClose, 180);
  };

  const handleConfirm = async () => {
    if (!selectedId || saving) return;
    const picked = others.find((p) => p.id === selectedId);
    if (!picked) return;
    setSaving(true);
    try {
      await onPick(picked.id, picked.name);
      handleClose();
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className={`fixed inset-0 flex items-center justify-center z-50 ${
        closing ? "anim-modal-overlay-out" : "anim-modal-overlay-in"
      }`}
    >
      <div
        className="absolute inset-0 cursor-pointer"
        onClick={handleClose}
        style={{ background: "rgba(0,0,0,0.45)" }}
      />

      <div
        className={`dialog-shell relative w-[420px] max-h-[70vh] overflow-hidden ${
          closing ? "anim-modal-out" : "anim-modal-in"
        }`}
      >
        <header className="flex items-center justify-between p-4 px-5 border-b border-(--border)">
          <div className="min-w-0">
            <h2 className="text-[14px] font-semibold text-(--t1)">プロジェクトを移動</h2>
            <p className="text-[11px] text-(--t3) mt-0.5 truncate">
              「{minutesTitle}」の移動先を選択
            </p>
          </div>
          <button type="button" onClick={handleClose} className="icon-btn" title="閉じる">
            <X size={14} weight="bold" />
          </button>
        </header>

        <div className="p-3 overflow-y-auto" style={{ maxHeight: "calc(70vh - 130px)" }}>
          {others.length === 0 ? (
            <p className="text-[12px] text-(--t3) text-center py-8">
              他に移動可能なプロジェクトがありません
            </p>
          ) : (
            <ul className="flex flex-col">
              {others.map((p) => {
                const selected = selectedId === p.id;
                return (
                  <li key={p.id}>
                    <button
                      type="button"
                      onClick={() => setSelectedId(p.id)}
                      className="w-full text-left flex items-center gap-2 px-3 py-2 rounded-md transition-colors"
                      style={{
                        background: selected ? "var(--hover)" : "transparent",
                      }}
                    >
                      <FolderOpen
                        size={13}
                        weight="regular"
                        className="text-(--t3) shrink-0"
                      />
                      <span className="flex-1 min-w-0 truncate text-[13px] text-(--t1)">
                        {p.name}
                      </span>
                      {selected && (
                        <Check size={12} weight="bold" className="text-(--accent) shrink-0" />
                      )}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        <footer className="flex items-center justify-end gap-1.5 p-4 px-5 border-t border-(--border)">
          <button
            type="button"
            onClick={handleClose}
            className="btn h-7 px-3 text-[11px]"
            disabled={saving}
          >
            キャンセル
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={!selectedId || saving || others.length === 0}
            className="btn btn-primary h-7 px-3 text-[11px]"
          >
            {saving ? "移動中..." : "移動"}
          </button>
        </footer>
      </div>
    </div>
  );
}
