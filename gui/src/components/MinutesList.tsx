import { useState, useEffect, useMemo } from "react";
import {
  MagnifyingGlass, X, DownloadSimple, Trash, Spinner as PhSpinner,
  WarningCircle, FolderOpen, ArrowSquareOut,
} from "@phosphor-icons/react";
import { ask } from "@tauri-apps/plugin-dialog";
import { showToast } from "../lib/toast";
import type {
  Minutes, MinutesSearchResult, PipelineStatus, Project,
} from "../lib/api";
import {
  searchMinutes, exportMinutes, deleteMinutes, moveMinutesToProject,
} from "../lib/api";
import { Spinner } from "./Spinner";
import { MoveToProjectModal } from "./MoveToProjectModal";

export type OpenMinutesOpts = {
  query?: string;
  tab?: "summary" | "transcript";
};

interface Props {
  minutes: Minutes[];
  projectId: string;
  allProjects: Project[];
  /** 進行中の要約ジョブ: minutes_id → state ("queued" / "running") */
  activeSummarizes: Map<string, string>;
  processing: PipelineStatus[];
  onOpenMin: (m: Minutes, opts?: OpenMinutesOpts) => void;
  onOpenPipeline: (sessionId: string) => void;
  onDismissPipeline: (sessionId: string) => void;
  onMutated: () => void;
}

const HL_OPEN = "";
const HL_CLOSE = "";

function fmtDur(s: number) {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h}時間${m}分` : `${m}分`;
}
function fmtTime(iso: string) {
  try {
    const d = new Date(iso);
    return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  } catch { return ""; }
}
function fmtDate(iso: string) {
  try {
    const d = new Date(iso);
    return `${d.getFullYear()}/${String(d.getMonth() + 1).padStart(2, "0")}/${String(d.getDate()).padStart(2, "0")}`;
  } catch { return ""; }
}
const WEEKDAYS = ["日", "月", "火", "水", "木", "金", "土"];
function fmtShortDate(iso: string) {
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    return `${d.getMonth() + 1}/${d.getDate()} (${WEEKDAYS[d.getDay()]})`;
  } catch { return ""; }
}
function startedAtTs(iso: string | undefined | null): number {
  if (!iso) return 0;
  const t = new Date(iso).getTime();
  return isNaN(t) ? 0 : t;
}

function summaryPreview(s: string | undefined, len = 80): string {
  if (!s) return "";
  return s
    .replace(/^#+\s+/gm, "")
    .replace(/^[-*]\s+/gm, "")
    .replace(/\[ \]|\[x\]/g, "")
    .replace(/\*\*(.+?)\*\*/g, "$1")
    .replace(/`(.+?)`/g, "$1")
    .replace(/\n+/g, " ")
    .trim()
    .slice(0, len);
}

function hasHighlight(s: string | null | undefined): boolean {
  return !!s && s.includes(HL_OPEN);
}

function HighlightedText({
  text,
  fallback,
}: {
  text: string | null | undefined;
  fallback?: string;
}) {
  if (!text) return <>{fallback ?? ""}</>;
  const parts: Array<{ text: string; mark: boolean }> = [];
  let cursor = 0;
  while (cursor < text.length) {
    const open = text.indexOf(HL_OPEN, cursor);
    if (open < 0) {
      parts.push({ text: text.slice(cursor), mark: false });
      break;
    }
    if (open > cursor) {
      parts.push({ text: text.slice(cursor, open), mark: false });
    }
    const close = text.indexOf(HL_CLOSE, open + 1);
    if (close < 0) {
      parts.push({ text: text.slice(open + 1), mark: true });
      break;
    }
    parts.push({ text: text.slice(open + 1, close), mark: true });
    cursor = close + 1;
  }
  return (
    <>
      {parts.map((p, i) =>
        p.mark ? <mark key={i} className="search-mark">{p.text}</mark> : <span key={i}>{p.text}</span>
      )}
    </>
  );
}

export function MinutesList({
  minutes, projectId, allProjects, activeSummarizes, processing,
  onOpenMin, onOpenPipeline, onDismissPipeline, onMutated,
}: Props) {
  const [q, setQ] = useState("");
  const [searched, setSearched] = useState<MinutesSearchResult[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [moveTarget, setMoveTarget] = useState<Minutes | MinutesSearchResult | null>(null);

  useEffect(() => { setQ(""); setSearched(null); }, [projectId]);

  useEffect(() => {
    const trimmed = q.trim();
    if (!trimmed) { setSearched(null); return; }
    setSearching(true);
    const id = setTimeout(async () => {
      try { setSearched(await searchMinutes(trimmed, projectId)); }
      catch { setSearched([]); }
      finally { setSearching(false); }
    }, 280);
    return () => clearTimeout(id);
  }, [q, projectId]);

  // 検索結果以外: パイプラインを minute 一致 / orphan に分類
  const { pipelineByMinSid, orphanPipelines } = useMemo(() => {
    const map = new Map<string, PipelineStatus>();
    const orphans: PipelineStatus[] = [];
    if (!searched) {
      const minSids = new Set(minutes.map((m) => m.session_id));
      for (const p of processing) {
        if (p.session_id && minSids.has(p.session_id)) {
          map.set(p.session_id, p);
        } else if (p.session_id) {
          orphans.push(p);
        }
      }
    }
    return { pipelineByMinSid: map, orphanPipelines: orphans };
  }, [processing, minutes, searched]);

  type Item =
    | { kind: "min"; m: Minutes; pipeline?: PipelineStatus }
    | { kind: "orphan"; p: PipelineStatus };

  const items = useMemo<Item[]>(() => {
    if (searched) return [];
    const arr: Item[] = minutes.map((m) => ({
      kind: "min" as const,
      m,
      pipeline: pipelineByMinSid.get(m.session_id),
    }));
    for (const p of orphanPipelines) {
      arr.push({ kind: "orphan" as const, p });
    }
    arr.sort((a, b) => {
      const ta = a.kind === "min" ? startedAtTs(a.m.started_at) : startedAtTs(a.p.started_at);
      const tb = b.kind === "min" ? startedAtTs(b.m.started_at) : startedAtTs(b.p.started_at);
      return tb - ta;
    });
    return arr;
  }, [minutes, searched, orphanPipelines, pipelineByMinSid]);

  const handleExport = async (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    if (busyId) return;
    setBusyId(id);
    try {
      const r = await exportMinutes(id);
      showToast({ kind: "ok", text: `保存: ${r.path.split("/").pop()}` });
    } catch (e) {
      showToast({ kind: "err", text: `書き出し失敗: ${e instanceof Error ? e.message : ""}` });
    } finally { setBusyId(null); }
  };

  const handleMovePick = async (targetProjectId: string, targetName: string) => {
    const target = moveTarget;
    if (!target) return;
    setBusyId(target.id);
    try {
      await moveMinutesToProject(target.id, targetProjectId);
      showToast({ kind: "ok", text: `「${targetName}」へ移動しました` });
      onMutated();
    } catch (e) {
      showToast({ kind: "err", text: `移動失敗: ${e instanceof Error ? e.message : ""}` });
    } finally { setBusyId(null); }
  };

  // 他に移動先となるプロジェクトがあるかどうか
  const hasOtherProjects = allProjects.some((p) => p.id !== projectId);

  const handleDelete = async (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    if (busyId) return;
    const ok = await ask("この議事録を削除しますか?", {
      title: "削除の確認",
      kind: "warning",
      okLabel: "削除",
      cancelLabel: "キャンセル",
    });
    if (!ok) return;
    setBusyId(id);
    try {
      await deleteMinutes(id);
      showToast({ kind: "ok", text: "削除しました" });
      onMutated();
    } catch (e) {
      showToast({ kind: "err", text: `削除失敗: ${e instanceof Error ? e.message : ""}` });
    } finally { setBusyId(null); }
  };

  const showSearchResults = searched !== null;
  const empty = !showSearchResults && items.length === 0;

  return (
    <div className="flex flex-col">
      {(minutes.length > 0 || showSearchResults) && (
        <div className="minutes-search-wrap">
          <div className="search-bar">
            <MagnifyingGlass size={13} weight="regular" className="text-(--t3) shrink-0" />
            <input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="タイトル・要約・本文を検索..."
              className="search-bar-input"
            />
            {searching && <Spinner size={12} />}
            {q && !searching && (
              <button
                onClick={() => setQ("")}
                className="icon-btn !w-5 !h-5"
                aria-label="クリア"
              >
                <X size={11} weight="bold" />
              </button>
            )}
          </div>
        </div>
      )}

      {empty && (
        <p className="text-[12px] text-(--t3) text-center py-12">
          録音するとここに議事録が表示されます
        </p>
      )}

      {showSearchResults && (
        <SearchResults
          query={q.trim()}
          results={searched!}
          searching={searching}
          busyId={busyId}
          canMove={hasOtherProjects}
          onOpenMin={onOpenMin}
          onExport={handleExport}
          onDelete={handleDelete}
          onStartMove={(m) => setMoveTarget(m)}
        />
      )}

      {!showSearchResults && (
        <div className="anim-fade-in">
          {items.map((it, idx) => {
            if (it.kind === "min") {
              return (
                <MinutesRow key={it.m.id}
                  m={it.m}
                  pipeline={it.pipeline}
                  summarizeState={activeSummarizes.get(it.m.id) ?? null}
                  busy={busyId === it.m.id}
                  canMove={hasOtherProjects}
                  onOpen={() => onOpenMin(it.m)}
                  onExport={(e) => handleExport(e, it.m.id)}
                  onDelete={(e) => handleDelete(e, it.m.id)}
                  onStartMove={() => setMoveTarget(it.m)} />
              );
            }
            return (
              <ProcessingRow key={it.p.session_id ?? `o${idx}`}
                p={it.p}
                onOpen={() => it.p.session_id && onOpenPipeline(it.p.session_id)}
                onDismiss={() => it.p.session_id && onDismissPipeline(it.p.session_id)} />
            );
          })}
        </div>
      )}

      {moveTarget && (
        <MoveToProjectModal
          minutesTitle={moveTarget.title}
          currentProjectId={projectId}
          projects={allProjects}
          onClose={() => setMoveTarget(null)}
          onPick={handleMovePick}
        />
      )}
    </div>
  );
}

function SearchResults({
  query, results, searching, busyId, canMove,
  onOpenMin, onExport, onDelete, onStartMove,
}: {
  query: string;
  results: MinutesSearchResult[];
  searching: boolean;
  busyId: string | null;
  canMove: boolean;
  onOpenMin: (m: Minutes, opts?: OpenMinutesOpts) => void;
  onExport: (e: React.MouseEvent, id: string) => void;
  onDelete: (e: React.MouseEvent, id: string) => void;
  onStartMove: (m: MinutesSearchResult) => void;
}) {
  if (results.length === 0) {
    return (
      <div className="px-5 py-12 text-center">
        <p className="text-[12px] text-(--t3)">
          {searching ? "検索中..." : <>「{query}」に一致する議事録は見つかりませんでした</>}
        </p>
      </div>
    );
  }
  return (
    <div className="px-5 py-2">
      <p className="text-[10px] font-semibold tracking-wider uppercase text-(--t3) mb-3">
        検索結果 {results.length} 件
      </p>
      <div className="flex flex-col gap-2">
        {results.map((r) => {
          const hl = r.highlights ?? { title: null, summary: null, transcript: null };
          const bestTab: "summary" | "transcript" | undefined =
            hasHighlight(hl.transcript) ? "transcript"
            : hasHighlight(hl.summary) ? "summary"
            : undefined;
          return (
            <SearchResultCard
              key={r.id}
              r={r}
              busy={busyId === r.id}
              canMove={canMove}
              onOpen={() => onOpenMin(r, { query, tab: bestTab })}
              onExport={(e) => onExport(e, r.id)}
              onDelete={(e) => onDelete(e, r.id)}
              onStartMove={() => onStartMove(r)}
            />
          );
        })}
      </div>
    </div>
  );
}

function SearchResultCard({
  r, busy, canMove, onOpen, onExport, onDelete, onStartMove,
}: {
  r: MinutesSearchResult;
  busy: boolean;
  canMove: boolean;
  onOpen: () => void;
  onExport: (e: React.MouseEvent) => void;
  onDelete: (e: React.MouseEvent) => void;
  onStartMove: () => void;
}) {
  const hl = r.highlights ?? { title: null, summary: null, transcript: null };
  const matchedFields: Array<{ key: "title" | "summary" | "transcript"; label: string }> = [];
  if (hasHighlight(hl.title)) matchedFields.push({ key: "title", label: "タイトル" });
  if (hasHighlight(hl.summary)) matchedFields.push({ key: "summary", label: "要約" });
  if (hasHighlight(hl.transcript)) matchedFields.push({ key: "transcript", label: "本文" });

  // 表示する snippet 優先度: 本文 > 要約 > タイトル
  const snippet =
    hasHighlight(hl.transcript) ? hl.transcript
    : hasHighlight(hl.summary) ? hl.summary
    : null;
  const snippetLabel =
    hasHighlight(hl.transcript) ? "本文"
    : hasHighlight(hl.summary) ? "要約"
    : null;

  return (
    <div onClick={onOpen} className="search-result-card group">
      <div className="flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h4 className="text-[13px] font-medium text-(--t1) min-w-0">
              <HighlightedText text={hl.title} fallback={r.title} />
            </h4>
            {matchedFields.map((f) => (
              <span key={f.key} className="match-badge">{f.label}</span>
            ))}
          </div>
          <p className="text-[10px] text-(--t3) mt-1 num">
            {fmtDate(r.date)} · {fmtTime(r.started_at)} · {fmtDur(r.duration_sec)}
          </p>
          {snippet && (
            <div className="mt-2 text-[12px] text-(--t2) leading-relaxed">
              {snippetLabel && (
                <span className="text-[10px] text-(--t3) mr-1.5">{snippetLabel}:</span>
              )}
              <HighlightedText text={snippet} />
            </div>
          )}
        </div>
        <div className="flat-row-actions flex items-center gap-0.5 shrink-0">
          {canMove && (
            <button
              onClick={(e) => { e.stopPropagation(); onStartMove(); }}
              disabled={busy}
              className="icon-btn !w-7 !h-7"
              title="他のプロジェクトへ移動"
            >
              <FolderOpen size={12} weight="regular" />
            </button>
          )}
          <button onClick={onExport} disabled={busy}
            className="icon-btn !w-7 !h-7" title="Markdown を書き出し">
            {busy ? <PhSpinner size={11} className="anim-spin" /> : <DownloadSimple size={12} weight="regular" />}
          </button>
          <button onClick={onDelete} disabled={busy}
            className="icon-btn !w-7 !h-7 hover:!text-(--danger)" title="削除">
            <Trash size={12} weight="regular" />
          </button>
        </div>
      </div>
    </div>
  );
}

function ProcessingRow({
  p, onOpen, onDismiss,
}: {
  p: PipelineStatus;
  onOpen: () => void;
  onDismiss: () => void;
}) {
  const isError = p.state === "error";
  const startedAt = p.started_at ? new Date(p.started_at) : null;
  const time = startedAt
    ? `${String(startedAt.getHours()).padStart(2, "0")}:${String(startedAt.getMinutes()).padStart(2, "0")}`
    : "";
  const shortDate = p.started_at ? fmtShortDate(p.started_at) : "";
  const title = `${time} の会議`;

  type Phase = "active" | "leaving";
  const computeInitial = (): Phase =>
    !isError && p.state === "done" ? "leaving" : "active";
  const [phase, setPhase] = useState<Phase>(computeInitial);

  useEffect(() => {
    if (isError) { setPhase("active"); return; }
    if (p.state === "done" && phase === "active") {
      setPhase("leaving");
      const t = setTimeout(() => onDismiss(), 380);
      return () => clearTimeout(t);
    }
  }, [p.state, isError, phase, onDismiss]);

  const activeLabel =
    p.state === "stopping" ? "停止処理中"
      : "文字起こし中";

  return (
    <div className="minutes-card proc-card group"
      data-anim={phase}
      onClick={onOpen}>
      <div className="minutes-card-body">
        <div className="minutes-card-meta num">
          {shortDate && <span>{shortDate}</span>}
          {shortDate && time && <span className="minutes-card-meta-sep">·</span>}
          {time && <span>{time}</span>}
        </div>
        <h4 className="minutes-card-title">{title}</h4>
        <p className={`minutes-card-status ${isError ? "is-error" : ""}`}>
          {isError ? (
            <WarningCircle size={11} weight="fill" />
          ) : (
            <Spinner size={10} color="var(--accent)" />
          )}
          <span className="truncate">{p.message || activeLabel}</span>
        </p>
      </div>

      {isError && (
        <div className="minutes-card-actions">
          <button
            onClick={(e) => { e.stopPropagation(); onOpen(); }}
            className="icon-btn !w-7 !h-7"
            title="詳細を開く"
          >
            <ArrowSquareOut size={11} weight="regular" />
          </button>
          <button onClick={(e) => { e.stopPropagation(); onDismiss(); }}
            className="icon-btn !w-7 !h-7"
            title="リストから消す">
            <X size={11} weight="bold" />
          </button>
        </div>
      )}
    </div>
  );
}

function MinutesRow({
  m, pipeline, summarizeState, busy, canMove,
  onOpen, onExport, onDelete, onStartMove,
}: {
  m: Minutes;
  pipeline?: PipelineStatus;
  summarizeState: string | null;
  busy: boolean;
  canMove: boolean;
  onOpen: () => void;
  onExport: (e: React.MouseEvent) => void;
  onDelete: (e: React.MouseEvent) => void;
  onStartMove: () => void;
}) {
  const preview = summaryPreview(m.summary, 140);
  const time = fmtTime(m.started_at);
  const shortDate = fmtShortDate(m.date || m.started_at);
  const isProcessing = pipeline && (
    pipeline.state === "stopping" || pipeline.state === "transcribing"
  );
  const isError = pipeline?.state === "error";
  const isSummarizing = !isProcessing && !isError && summarizeState !== null
    && (summarizeState === "queued" || summarizeState === "running");

  const statusText = (() => {
    if (pipeline) {
      if (isError) return pipeline.message || "エラー";
      if (pipeline.state === "stopping") return pipeline.message || "停止処理中";
      if (pipeline.state === "transcribing") return pipeline.message || "再文字起こし中";
    }
    if (isSummarizing) {
      return summarizeState === "queued" ? "要約キュー待ち" : "要約中";
    }
    return null;
  })();

  return (
    <div onClick={onOpen} className="minutes-card group">
      <div className="minutes-card-body">
        <div className="minutes-card-meta num">
          {shortDate && <span>{shortDate}</span>}
          {shortDate && time && <span className="minutes-card-meta-sep">·</span>}
          {time && <span>{time}</span>}
          {m.duration_sec > 0 && <>
            <span className="minutes-card-meta-sep">·</span>
            <span>{fmtDur(m.duration_sec)}</span>
          </>}
        </div>
        <h4 className="minutes-card-title">{m.title}</h4>
        {statusText ? (
          <p className={`minutes-card-status ${isError ? "is-error" : ""}`}>
            {(isProcessing || isSummarizing) && <Spinner size={10} color="var(--accent)" />}
            {isError && <WarningCircle size={11} weight="fill" />}
            <span className="truncate">{statusText}</span>
          </p>
        ) : preview ? (
          <p className="minutes-card-preview">{preview}</p>
        ) : (
          <p className="minutes-card-preview is-empty">要約待ち</p>
        )}
      </div>
      <div className="minutes-card-actions">
        {canMove && (
          <RowAction
            onClick={(e) => { e.stopPropagation(); onStartMove(); }}
            title="他のプロジェクトへ移動"
            busy={busy}
          >
            <FolderOpen size={12} weight="regular" />
          </RowAction>
        )}
        <RowAction onClick={onExport} title="Markdown を書き出し" busy={busy}>
          <DownloadSimple size={12} weight="regular" />
        </RowAction>
        <RowAction onClick={onDelete} title="削除" danger busy={busy}>
          <Trash size={12} weight="regular" />
        </RowAction>
      </div>
    </div>
  );
}

function RowAction({
  children, onClick, title, danger, busy,
}: {
  children: React.ReactNode;
  onClick: (e: React.MouseEvent) => void;
  title: string;
  danger?: boolean;
  busy?: boolean;
}) {
  return (
    <button onClick={onClick} title={title} disabled={busy}
      className={`icon-btn !w-7 !h-7 ${danger ? "hover:!text-(--danger)" : ""}`}>
      {busy ? <PhSpinner size={11} className="anim-spin" /> : children}
    </button>
  );
}
