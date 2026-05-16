import { useState, useMemo } from "react";
import { MagnifyingGlass, Plus, Gear, SlidersHorizontal } from "@phosphor-icons/react";
import type { Project } from "../lib/api";
import { PulseDot } from "./PulseDot";

interface Props {
  projects: Project[];
  selected: Project | null;
  editingProjectId: string | null;
  settingsActive: boolean;
  /** タスクが進行中の project_id 集合(録音/文字起こし/要約)。 */
  activeProjectIds?: Set<string>;
  onSelect: (p: Project) => void;
  onNewProject: () => void;
  onOpenProjectSettings: (p: Project) => void;
  onOpenSettings: () => void;
}

export function Sidebar({
  projects, selected, editingProjectId, settingsActive, activeProjectIds,
  onSelect, onNewProject, onOpenProjectSettings, onOpenSettings,
}: Props) {
  const [q, setQ] = useState("");

  const filtered = useMemo(() => {
    const s = q.trim().toLowerCase();
    if (!s) return projects;
    return projects.filter((p) => p.name.toLowerCase().includes(s));
  }, [projects, q]);

  return (
    <aside className="sidebar-shell">
      {/* search */}
      <div className="px-3 pt-3 pb-2 relative">
        <MagnifyingGlass size={12} weight="regular"
          className="absolute left-[22px] top-1/2 -translate-y-1/2 text-(--t3) pointer-events-none" />
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="プロジェクト検索"
          className="sidebar-search"
        />
      </div>

      {/* project list */}
      <div className="flex-1 overflow-y-auto pb-2">
        {filtered.length === 0 ? (
          <p className="text-[11px] text-(--t3) text-center py-6 px-3">
            {q ? "見つかりません" : "プロジェクトなし"}
          </p>
        ) : (
          <div className="flex flex-col">
            {filtered.map((p) => {
              const active = p.id === selected?.id;
              const editing = p.id === editingProjectId;
              const busy = activeProjectIds?.has(p.id) ?? false;
              return (
                <div
                  key={p.id}
                  className={`project-row group ${active ? "active" : ""} ${editing ? "editing" : ""}`}
                  title={p.name}
                >
                  <button
                    onClick={() => onSelect(p)}
                    className="flex-1 min-w-0 truncate text-left bg-transparent border-none p-0 cursor-pointer flex items-center gap-1.5"
                    style={{ font: "inherit", color: "inherit" }}
                  >
                    <span className="truncate flex-1 min-w-0">{p.name}</span>
                    {busy && (
                      <span title="タスク実行中" className="shrink-0">
                        <PulseDot size={9} color="var(--accent)" />
                      </span>
                    )}
                  </button>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onOpenProjectSettings(p);
                    }}
                    className={`project-row-action ${editing ? "is-editing" : ""}`}
                    title={`${p.name} の設定`}
                    aria-label={`${p.name} の設定`}
                  >
                    <SlidersHorizontal size={12} weight={editing ? "bold" : "regular"} />
                  </button>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* footer: new project + global settings */}
      <div className="border-t border-(--border) p-2 flex items-center gap-1">
        <button
          onClick={onNewProject}
          className="flex-1 flex items-center justify-center gap-1.5 py-1.5 text-[11px] text-(--t3) hover:text-(--t1) hover:bg-(--surface) rounded-md cursor-pointer transition-colors"
          style={{ background: "transparent", border: "none" }}>
          <Plus size={12} weight="bold" />
          新規プロジェクト
        </button>
        <button
          onClick={onOpenSettings}
          className={`icon-btn ${settingsActive ? "is-active" : ""}`}
          title="全体設定">
          <Gear size={14} weight={settingsActive ? "fill" : "regular"} />
        </button>
      </div>
    </aside>
  );
}
