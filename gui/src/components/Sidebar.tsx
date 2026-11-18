import { useEffect, useLayoutEffect, useRef, useState, type PointerEvent, type CSSProperties } from "react";
import { Folder, Gear, MagnifyingGlass, Plus, SlidersHorizontal, X } from "@phosphor-icons/react";
import type { Project } from "../lib/api";

interface Props {
  projects: Project[];
  selected: Project | null;
  editingProjectId: string | null;
  settingsActive: boolean;
  activeProjectIds: Set<string>;
  onSelect: (p: Project) => void;
  onNewProject: () => void;
  onReorder: (projectIds: string[]) => Promise<void>;
  onOpenProjectSettings: (p: Project) => void;
  onOpenSettings: () => void;
}

const ROW_HEIGHT = 40;

interface Drag {
  pointerId: number;
  from: number;
  to: number;
  startX: number;
  startY: number;
  startScroll: number;
  clientY: number;
  offset: number;
  active: boolean;
}

export function Sidebar({
  projects, selected, editingProjectId, settingsActive, activeProjectIds,
  onSelect, onNewProject, onReorder, onOpenProjectSettings, onOpenSettings,
}: Props) {
  const [query, setQuery] = useState("");
  const [preview, setPreview] = useState<Drag | null>(null);
  const [saving, setSaving] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const list = useRef<HTMLDivElement>(null);
  const drag = useRef<Drag | null>(null);
  const frame = useRef(0);
  const suppressClick = useRef(false);
  const orderPending = useRef(false);
  const slots = useRef(new Map<string, HTMLDivElement>());
  const beforeMove = useRef<Map<string, number> | null>(null);

  const filter = query.trim().toLocaleLowerCase();
  const visible = projects.filter((project) => project.name.toLocaleLowerCase().includes(filter));
  const reorderable = visible.length > 1 && !saving;

  useEffect(() => () => cancelAnimationFrame(frame.current), []);

  useLayoutEffect(() => {
    const prior = beforeMove.current;
    beforeMove.current = null;
    if (!prior || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    for (const [id, node] of slots.current) {
      const top = prior.get(id);
      if (top === undefined) continue;
      const offset = top - node.getBoundingClientRect().top;
      if (Math.abs(offset) < 1) continue;
      node.animate([{ transform: `translateY(${offset}px)` }, { transform: "translateY(0)" }], {
        duration: 180, easing: "cubic-bezier(0.22, 1, 0.36, 1)",
      });
    }
  }, [projects]);

  async function move(from: number, to: number) {
    if (from === to || orderPending.current) return;
    const ordered = visible.map((p) => p.id);
    const [moved] = ordered.splice(from, 1);
    if (!moved) return;
    ordered.splice(to, 0, moved);
    const visibleIds = new Set(ordered);
    let index = 0;
    const ids = projects.map((p) => visibleIds.has(p.id) ? ordered[index++] : p.id);

    beforeMove.current = new Map([...slots.current].map(([id, node]) => [id, node.getBoundingClientRect().top]));
    orderPending.current = true;
    setSaving(true);
    try {
      await onReorder(ids);
      setAnnouncement(`${visible[from].name}を${to + 1}番目に移動しました`);
    } catch {
      setAnnouncement("並び順を保存できませんでした");
    } finally {
      orderPending.current = false;
      setSaving(false);
    }
  }

  function begin(event: PointerEvent, index: number) {
    suppressClick.current = false;
    if (!event.isPrimary || event.button !== 0 || !reorderable) return;
    if (event.target instanceof Element && event.target.closest(".project-row-action")) return;
    drag.current = {
      pointerId: event.pointerId, from: index, to: index,
      startX: event.clientX, startY: event.clientY, clientY: event.clientY,
      startScroll: list.current?.scrollTop ?? 0, offset: 0, active: false,
    };
  }

  function updateDrag(current: Drag, scrollTop: number) {
    current.offset = current.clientY - current.startY + scrollTop - current.startScroll;
    current.offset = Math.min((visible.length - 1 - current.from) * ROW_HEIGHT + 8, Math.max(-current.from * ROW_HEIGHT - 8, current.offset));
    current.to = Math.min(visible.length - 1, Math.max(0, current.from + Math.round(current.offset / ROW_HEIGHT)));
    setPreview({ ...current });
  }

  function scrollWhileDragging() {
    const current = drag.current;
    const viewport = list.current;
    if (!current?.active || !viewport) return;
    const rect = viewport.getBoundingClientRect();
    const delta = current.clientY < rect.top + 28 ? -8 : current.clientY > rect.bottom - 28 ? 8 : 0;
    if (delta) {
      const top = viewport.scrollTop;
      viewport.scrollTop += delta;
      if (top !== viewport.scrollTop) updateDrag(current, viewport.scrollTop);
    }
    frame.current = requestAnimationFrame(scrollWhileDragging);
  }

  function follow(event: PointerEvent<HTMLDivElement>) {
    const current = drag.current;
    if (!current || current.pointerId !== event.pointerId) return;
    if (!current.active) {
      if (Math.hypot(event.clientX - current.startX, event.clientY - current.startY) < 5) return;
      current.active = true;
      suppressClick.current = true;
      event.currentTarget.setPointerCapture(event.pointerId);
      frame.current = requestAnimationFrame(scrollWhileDragging);
    }
    current.clientY = event.clientY;
    updateDrag(current, event.currentTarget.scrollTop);
  }

  function finish(commit: boolean) {
    const current = drag.current;
    if (!current) return;
    drag.current = null;
    cancelAnimationFrame(frame.current);
    if (list.current?.hasPointerCapture(current.pointerId)) {
      list.current.releasePointerCapture(current.pointerId);
    }
    setPreview(null);
    if (commit && current.active) void move(current.from, current.to);
  }

  function rowOffset(index: number) {
    if (!preview) return 0;
    if (index === preview.from) return preview.offset;
    if (index > preview.from && index <= preview.to) return -ROW_HEIGHT;
    if (index < preview.from && index >= preview.to) return ROW_HEIGHT;
    return 0;
  }

  return (
    <aside className="sidebar-shell" aria-label="プロジェクト">
      <div className="sidebar-project-heading">
        <h2>プロジェクト</h2>
        <button type="button" className="icon-btn" onClick={onNewProject} title="新規プロジェクト" aria-label="新規プロジェクト">
          <Plus size={16} />
        </button>
      </div>
      <div className="project-filter">
        <MagnifyingGlass size={16} aria-hidden />
        <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="プロジェクトを検索" aria-label="プロジェクトを検索" />
        {query && <button type="button" className="icon-btn" onClick={() => setQuery("")} aria-label="検索をクリア"><X size={14} /></button>}
      </div>
      <div
        ref={list}
        className="project-list-scroll"
        onPointerMove={follow}
        onPointerUp={() => finish(true)}
        onPointerCancel={() => finish(false)}
        onLostPointerCapture={() => finish(false)}
        onPointerLeave={() => { if (!drag.current?.active) finish(false); }}
        onKeyDown={(event) => {
          if (event.key === "Escape" && drag.current) {
            event.preventDefault();
            finish(false);
          }
        }}
      >
        {visible.length === 0 ? (
          <p className="project-list-empty">{filter ? "該当するプロジェクトがありません" : "プロジェクトなし"}</p>
        ) : (
          <div className="project-list" data-sorting={Boolean(preview) || undefined} role="list" aria-label="プロジェクト一覧" style={{ "--project-row-height": `${ROW_HEIGHT}px` } as CSSProperties}>
            {visible.map((p, index) => {
              const active = p.id === selected?.id;
              const picked = preview?.from === index;
              return (
                <div
                  key={p.id}
                  role="listitem"
                  ref={(node) => {
                    if (node) slots.current.set(p.id, node);
                    else slots.current.delete(p.id);
                  }}
                  className="project-slot"
                  data-picked={picked || undefined}
                  style={{ transform: `translateY(${rowOffset(index)}px)` }}
                  onPointerDown={(event) => begin(event, index)}
                >
                  <div className="project-item" data-selected={active || undefined} data-editing={p.id === editingProjectId || undefined}>
                    <button
                      type="button"
                      className="project-select"
                      aria-current={active ? "page" : undefined}
                      aria-keyshortcuts={reorderable ? "Alt+ArrowUp Alt+ArrowDown" : undefined}
                      onClick={(event) => {
                        if (event.detail && suppressClick.current) return;
                        onSelect(p);
                      }}
                      onKeyDown={(event) => {
                        if (!reorderable || !event.altKey || event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
                        event.preventDefault();
                        const to = index + (event.key === "ArrowUp" ? -1 : 1);
                        if (to >= 0 && to < visible.length) void move(index, to);
                      }}
                      title={p.name}
                    >
                      <Folder size={16} aria-hidden />
                      <span className="truncate">{p.name}</span>
                      {activeProjectIds.has(p.id) && <span className="project-busy" aria-label="処理中" title="処理中" />}
                    </button>
                    <button type="button" className="icon-btn project-row-action" onClick={() => onOpenProjectSettings(p)} aria-label={`${p.name}の設定`} title={`${p.name}の設定`}>
                      <SlidersHorizontal size={16} />
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
      <span className="sr-only" aria-live="polite">{announcement}</span>
      <div className="sidebar-footer">
        <button type="button" onClick={onOpenSettings} className="sidebar-settings" aria-pressed={settingsActive}>
          <Gear size={16} /> 設定
        </button>
      </div>
    </aside>
  );
}
