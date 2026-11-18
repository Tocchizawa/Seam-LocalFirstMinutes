import { useState, useMemo, useRef, useLayoutEffect, type DragEvent } from "react";
import {
  MagnifyingGlass, Plus, Gear, SlidersHorizontal, ArrowsDownUp,
} from "@phosphor-icons/react";
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
  onReorder: (projectIds: string[]) => Promise<void>;
  onOpenProjectSettings: (p: Project) => void;
  onOpenSettings: () => void;
}

export function Sidebar({
  projects, selected, editingProjectId, settingsActive, activeProjectIds,
  onSelect, onNewProject, onReorder, onOpenProjectSettings, onOpenSettings,
}: Props) {
  const [q, setQ] = useState("");
  const [previewIds, setPreviewIds] = useState<string[] | null>(null);
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const [dropTarget, setDropTarget] = useState<{ id: string; position: "before" | "after" } | null>(null);
  const [savingOrder, setSavingOrder] = useState(false);
  const [orderAnnouncement, setOrderAnnouncement] = useState("");
  const rowRefs = useRef(new Map<string, HTMLDivElement>());
  const previousRects = useRef<Map<string, DOMRect> | null>(null);
  const flipAnimations = useRef(new Map<string, Animation>());
  const dropCommitted = useRef(false);

  const reordered = useMemo(() => {
    if (!previewIds) return projects;
    const byId = new Map<string, Project>(
      projects.map((project) => [project.id, project] as const),
    );
    const ordered = previewIds.flatMap((id) => {
      const project = byId.get(id);
      return project ? [project] : [];
    });
    const included = new Set(ordered.map((project) => project.id));
    return [...ordered, ...projects.filter((project) => !included.has(project.id))];
  }, [projects, previewIds]);

  const filtered = useMemo(() => {
    const s = q.trim().toLowerCase();
    if (!s) return reordered;
    return reordered.filter((p) => p.name.toLowerCase().includes(s));
  }, [reordered, q]);

  const reorderable = !q.trim() && projects.length > 1 && !savingOrder;

  useLayoutEffect(() => {
    const firstRects = previousRects.current;
    previousRects.current = null;
    if (!firstRects || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    for (const [id, element] of rowRefs.current) {
      const before = firstRects.get(id);
      if (!before) continue;
      const after = element.getBoundingClientRect();
      const offset = before.top - after.top;
      if (Math.abs(offset) < 1) continue;
      flipAnimations.current.get(id)?.cancel();
      const animation = element.animate(
        [
          { transform: `translateY(${offset}px)` },
          { transform: "translateY(0)" },
        ],
        { duration: 210, easing: "cubic-bezier(0.2, 0.75, 0.25, 1)" },
      );
      flipAnimations.current.set(id, animation);
      animation.onfinish = () => {
        if (flipAnimations.current.get(id) === animation) flipAnimations.current.delete(id);
      };
    }
  }, [previewIds]);

  const previewOrder = (nextIds: string[]) => {
    previousRects.current = new Map<string, DOMRect>(
      [...rowRefs.current].map(([id, element]) => [id, element.getBoundingClientRect()] as const),
    );
    setPreviewIds(nextIds);
  };

  const commitOrder = async (projectIds: string[]) => {
    setSavingOrder(true);
    try {
      await onReorder(projectIds);
    } catch (error) {
      console.warn("[projects] reorder request failed", error);
    } finally {
      setPreviewIds(null);
      setSavingOrder(false);
    }
  };

  const moveProject = (projectId: string, delta: -1 | 1) => {
    const ids = reordered.map((project) => project.id);
    const index = ids.indexOf(projectId);
    const targetIndex = index + delta;
    if (index < 0 || targetIndex < 0 || targetIndex >= ids.length || !reorderable) return;
    [ids[index], ids[targetIndex]] = [ids[targetIndex], ids[index]];
    setOrderAnnouncement(`${reordered[index].name} を ${targetIndex + 1} 番目に移動しました`);
    previewOrder(ids);
    void commitOrder(ids);
  };

  const handleDragOver = (
    event: DragEvent<HTMLDivElement>,
    targetId: string,
  ) => {
    if (!draggingId || !reorderable) return;
    if (draggingId === targetId) {
      setDropTarget(null);
      return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    const rect = event.currentTarget.getBoundingClientRect();
    const position = event.clientY < rect.top + rect.height / 2 ? "before" : "after";
    setDropTarget((previous) => (
      previous?.id === targetId && previous.position === position
        ? previous
        : { id: targetId, position }
    ));

    const ids = reordered.map((project) => project.id).filter((id) => id !== draggingId);
    let insertAt = ids.indexOf(targetId);
    if (insertAt < 0) return;
    if (position === "after") insertAt += 1;
    ids.splice(insertAt, 0, draggingId);
    if (ids.some((id, index) => id !== reordered[index]?.id)) previewOrder(ids);
  };

  const finishDrop = (commit: boolean) => {
    const orderChanged = previewIds?.some((id, index) => id !== projects[index]?.id) ?? false;
    if (commit && !dropCommitted.current && draggingId && orderChanged && previewIds) {
      dropCommitted.current = true;
      void commitOrder(previewIds);
    }
    setDraggingId(null);
    setDropTarget(null);
    if (!commit || !previewIds) setPreviewIds(null);
  };

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
        {projects.length > 1 && (
          <div
            className={`project-order-hint ${q.trim() ? "is-filtered" : ""}`}
            role={savingOrder || draggingId ? "status" : undefined}
            aria-live="polite"
          >
            <ArrowsDownUp size={12} weight="regular" aria-hidden="true" />
            <span>
              {q.trim()
                ? "検索をクリアすると並び替えできます"
                : savingOrder
                  ? "並び順を保存中..."
                  : draggingId
                    ? "移動先で離して配置"
                    : "ドラッグで並べ替え · ⌥ + ↑↓"}
            </span>
          </div>
        )}
        {filtered.length === 0 ? (
          <p className="text-[11px] text-(--t3) text-center py-6 px-3">
            {q ? "見つかりません" : "プロジェクトなし"}
          </p>
        ) : (
          <div className="flex flex-col" role="list" aria-label="プロジェクト一覧">
            {filtered.map((p, index) => {
              const active = p.id === selected?.id;
              const editing = p.id === editingProjectId;
              const busy = activeProjectIds?.has(p.id) ?? false;
              const dropPosition = dropTarget?.id === p.id ? dropTarget.position : null;
              return (
                <div
                  key={p.id}
                  ref={(node) => {
                    if (node) rowRefs.current.set(p.id, node);
                    else rowRefs.current.delete(p.id);
                  }}
                  draggable={reorderable}
                  role="listitem"
                  onDragStart={(event) => {
                    if (!reorderable || (event.target as HTMLElement).closest(".project-row-action")) {
                      event.preventDefault();
                      return;
                    }
                    event.dataTransfer.effectAllowed = "move";
                    event.dataTransfer.setData("text/plain", p.id);
                    dropCommitted.current = false;
                    setDraggingId(p.id);
                    setPreviewIds(projects.map((project) => project.id));
                  }}
                  onDragOver={(event) => handleDragOver(event, p.id)}
                  onDrop={(event) => {
                    event.preventDefault();
                    finishDrop(true);
                  }}
                  onDragEnd={() => finishDrop(false)}
                  onKeyDown={(event) => {
                    if ((event.target as HTMLElement).closest(".project-row-action")) return;
                    if (!event.altKey || event.shiftKey || !reorderable) return;
                    if (event.key === "ArrowUp" || event.key === "ArrowDown") {
                      event.preventDefault();
                      moveProject(p.id, event.key === "ArrowUp" ? -1 : 1);
                    }
                  }}
                  className={`project-row group ${active ? "active" : ""} ${editing ? "editing" : ""} ${reorderable ? "is-reorderable" : ""} ${draggingId === p.id ? "is-dragging" : ""} ${dropPosition ? `drop-${dropPosition}` : ""}`}
                  title={p.name}
                >
                  <button
                    onClick={() => onSelect(p)}
                    aria-keyshortcuts={reorderable ? "Alt+ArrowUp Alt+ArrowDown" : undefined}
                    aria-label={`${p.name}、${index + 1} / ${filtered.length}`}
                    aria-pressed={active}
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
        <span className="sr-only" aria-live="polite">{orderAnnouncement}</span>
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
