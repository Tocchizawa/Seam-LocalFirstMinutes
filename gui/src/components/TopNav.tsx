import { useEffect, useRef, useState } from "react";
import { CaretDown, Gear, Plus } from "@phosphor-icons/react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import type { Project } from "../lib/api";

interface Props {
  projects: Project[];
  selected: Project | null;
  onSelectProject: (p: Project) => void;
  onNewProject: () => void;
  onOpenSettings: () => void;
}

export function TopNav({ projects, selected, onSelectProject, onNewProject, onOpenSettings }: Props) {
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      const t = e.target as Node;
      if (menuRef.current && !menuRef.current.contains(t)) setMenuOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  const handleDrag = (e: React.MouseEvent) => {
    if (e.button !== 0) return;
    if ((e.target as HTMLElement).closest("button, [role=menu]")) return;
    e.preventDefault();
    getCurrentWindow().startDragging();
  };

  return (
    <div data-tauri-drag-region onMouseDown={handleDrag}
      className="titlebar shrink-0 h-9 flex items-center justify-end pr-1 relative">

      {/* 中央: プロジェクト名 (クリックで切替メニュー) */}
      <div ref={menuRef} className="absolute left-1/2 -translate-x-1/2">
        <button onClick={() => setMenuOpen((o) => !o)}
          className="flex items-center gap-1 px-2 py-1 rounded-md text-[12px] font-semibold text-(--t1) bg-transparent hover:bg-(--surface) border-none cursor-pointer transition-colors"
          aria-haspopup="menu"
          aria-expanded={menuOpen}>
          <span>{selected?.name || "Seam"}</span>
          <CaretDown size={10} weight="bold" className="text-(--t3)" />
        </button>

        {menuOpen && (
          <div role="menu"
            className="anim-fade-in absolute top-full left-1/2 -translate-x-1/2 mt-1 min-w-[220px] dialog-shell py-1 z-50">
            <div className="max-h-[280px] overflow-y-auto py-1">
              {projects.length === 0 && (
                <div className="px-3 py-2 text-[11px] text-(--t3) text-center">
                  プロジェクトがありません
                </div>
              )}
              {projects.map((p) => {
                const active = p.id === selected?.id;
                return (
                  <button key={p.id}
                    onClick={() => { onSelectProject(p); setMenuOpen(false); }}
                    className={`w-full text-left px-3 py-1.5 text-[12px] cursor-pointer flex items-center gap-2 transition-colors hover:bg-(--surface-2) ${active ? "text-(--t1)" : "text-(--t2)"}`}
                    style={{ background: "transparent", border: "none" }}>
                    <span className="flex-1 truncate">{p.name}</span>
                    {active && <span className="w-1.5 h-1.5 rounded-full bg-(--blue)" />}
                  </button>
                );
              })}
            </div>
            <div className="border-t border-(--border) mt-1 pt-1">
              <button
                onClick={() => { onNewProject(); setMenuOpen(false); }}
                className="w-full text-left px-3 py-1.5 text-[12px] cursor-pointer flex items-center gap-2 text-(--t2) hover:text-(--t1) hover:bg-(--surface-2) transition-colors"
                style={{ background: "transparent", border: "none" }}>
                <Plus size={12} weight="bold" />
                新規プロジェクト
              </button>
            </div>
          </div>
        )}
      </div>

      {/* 右: 設定 */}
      <button onClick={onOpenSettings} className="icon-btn" title="設定">
        <Gear size={14} weight="regular" />
      </button>
    </div>
  );
}
