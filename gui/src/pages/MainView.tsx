import { useState, useEffect, useCallback, useRef } from "react";
import type {
  Project, Minutes, PipelineStatus,
} from "../lib/api";
import type { OpenMinutesOpts } from "../components/MinutesList";
import {
  listMinutes, getMinutes, dismissPipeline, getSettings, getDebugStatus,
} from "../lib/api";
import { MinutesList } from "../components/MinutesList";
import { useRecording } from "../lib/recording-context";

interface Props {
  project: Project;
  allProjects: Project[];
  /** 進行中の要約ジョブ: minutes_id → state */
  activeSummarizes: Map<string, string>;
  onOpenMinutes: (m: Minutes, opts?: OpenMinutesOpts) => void;
  onOpenPipelineSession: (sessionId: string) => void;
  pipelines: PipelineStatus[];
  refreshPipelines: () => void;
}

const MINUTES_PAGE_SIZE = 20;

export function MainView({
  project, allProjects, activeSummarizes,
  onOpenMinutes, onOpenPipelineSession, pipelines, refreshPipelines,
}: Props) {
  const { recording, streamStatus } = useRecording();

  const [minutes, setMinutes] = useState<Minutes[]>([]);
  const [minutesLoading, setMinutesLoading] = useState(false);
  const [minutesLoadingMore, setMinutesLoadingMore] = useState(false);
  const [minutesHasMore, setMinutesHasMore] = useState(false);
  const [minutesError, setMinutesError] = useState("");
  const [minutesMoreError, setMinutesMoreError] = useState("");
  const loadingMoreRef = useRef(false);
  const projectIdRef = useRef(project.id);
  const minutesRequestRef = useRef(0);
  const detailRequestRef = useRef(0);
  useEffect(() => () => { detailRequestRef.current += 1; }, []);
  projectIdRef.current = project.id;
  const [debugMode, setDebugMode] = useState(false);
  const [debugStatus, setDebugStatus] = useState<Record<string, any> | null>(null);
  const [debugError, setDebugError] = useState("");

  useEffect(() => {
    getSettings()
      .then((s) => setDebugMode(Boolean((s.debug as any)?.enabled)))
      .catch(() => setDebugMode(false));
  }, []);

  useEffect(() => {
    const onUpdated = (ev: Event) => {
      const custom = ev as CustomEvent<Record<string, any>>;
      const next = Boolean((custom.detail?.debug as any)?.enabled);
      setDebugMode(next);
      if (!next) {
        setDebugStatus(null);
        setDebugError("");
      }
    };
    window.addEventListener("settings-updated", onUpdated as EventListener);
    return () => window.removeEventListener("settings-updated", onUpdated as EventListener);
  }, []);

  useEffect(() => {
    if (!debugMode) return;
    let stopped = false;
    let timer: number | null = null;

    const loop = async () => {
      try {
        const dbg = await getDebugStatus(recording ? 180 : 100);
        if (!stopped) {
          setDebugStatus(dbg as Record<string, any>);
          setDebugError("");
        }
      } catch {
        if (!stopped) setDebugError("debug status の取得に失敗しました");
      } finally {
        if (!stopped) {
          timer = window.setTimeout(loop, recording ? 1600 : 2800);
        }
      }
    };

    loop();
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [debugMode, recording]);

  const loadInitialMinutes = useCallback(async () => {
    const requestId = ++minutesRequestRef.current;
    const requestedProjectId = project.id;
    loadingMoreRef.current = false;
    setMinutesLoadingMore(false);
    setMinutesLoading(true);
    setMinutesError("");
    setMinutesMoreError("");
    setMinutesHasMore(false);
    setMinutes([]);
    try {
      const page = await listMinutes(requestedProjectId, MINUTES_PAGE_SIZE, 0);
      if (requestId !== minutesRequestRef.current || projectIdRef.current !== requestedProjectId) return;
      setMinutes(page);
      setMinutesHasMore(page.length === MINUTES_PAGE_SIZE);
    } catch (e) {
      if (requestId !== minutesRequestRef.current || projectIdRef.current !== requestedProjectId) return;
      setMinutes([]);
      setMinutesHasMore(false);
      setMinutesError(e instanceof Error ? e.message : "議事録一覧の取得に失敗しました");
    } finally {
      if (requestId === minutesRequestRef.current && projectIdRef.current === requestedProjectId) {
        setMinutesLoading(false);
      }
    }
  }, [project.id]);

  const refreshMinutes = useCallback(async () => {
    const requestId = ++minutesRequestRef.current;
    const requestedProjectId = project.id;
    loadingMoreRef.current = false;
    setMinutesLoadingMore(false);
    const limit = Math.max(MINUTES_PAGE_SIZE, minutes.length || MINUTES_PAGE_SIZE);
    setMinutesMoreError("");
    try {
      const page = await listMinutes(requestedProjectId, limit, 0);
      if (requestId !== minutesRequestRef.current || projectIdRef.current !== requestedProjectId) return;
      setMinutes(page);
      setMinutesHasMore(page.length === limit);
      setMinutesError("");
    } catch (e) {
      if (requestId !== minutesRequestRef.current || projectIdRef.current !== requestedProjectId) return;
      if (minutes.length === 0) {
        setMinutes([]);
        setMinutesHasMore(false);
        setMinutesError(e instanceof Error ? e.message : "議事録一覧の取得に失敗しました");
      } else {
        setMinutesMoreError(e instanceof Error ? e.message : "議事録一覧を更新できませんでした");
      }
    } finally {
      if (requestId === minutesRequestRef.current && projectIdRef.current === requestedProjectId) {
        setMinutesLoading(false);
      }
    }
  }, [project.id, minutes.length]);

  const loadMoreMinutes = useCallback(async () => {
    if (loadingMoreRef.current || minutesLoading || minutesLoadingMore || !minutesHasMore) return;
    const requestId = ++minutesRequestRef.current;
    const requestedProjectId = project.id;
    loadingMoreRef.current = true;
    setMinutesLoadingMore(true);
    setMinutesMoreError("");
    try {
      const page = await listMinutes(requestedProjectId, MINUTES_PAGE_SIZE, minutes.length);
      if (requestId !== minutesRequestRef.current || projectIdRef.current !== requestedProjectId) return;
      setMinutes((prev) => {
        const seen = new Set(prev.map((m) => m.id));
        const unique = page.filter((m) => !seen.has(m.id));
        return unique.length > 0 ? [...prev, ...unique] : prev;
      });
      setMinutesHasMore(page.length === MINUTES_PAGE_SIZE);
    } catch (e) {
      if (requestId !== minutesRequestRef.current || projectIdRef.current !== requestedProjectId) return;
      setMinutesMoreError(e instanceof Error ? e.message : "過去の議事録を読み込めませんでした");
    } finally {
      if (requestId === minutesRequestRef.current && projectIdRef.current === requestedProjectId) {
        loadingMoreRef.current = false;
        setMinutesLoadingMore(false);
      }
    }
  }, [project.id, minutes.length, minutesHasMore, minutesLoading, minutesLoadingMore]);

  useEffect(() => {
    loadingMoreRef.current = false;
    setMinutesLoadingMore(false);
    loadInitialMinutes();
  }, [loadInitialMinutes]);

  const refreshMinutesAndPipelines = useCallback(() => {
    void refreshMinutes();
    refreshPipelines();
  }, [refreshMinutes, refreshPipelines]);

  useEffect(() => {
    const events = [
      "minutes-updated",
      ...[
        "__open", "pipeline_done", "minutes_saved", "session_recovery_finished", "summary_done",
        "summary_failed", "summary_cancelled", "summary_skipped",
        "transcript_corrected", "minutes_title_updated",
      ].map((name) => `recording-ws:${name}`),
    ];
    events.forEach((event) => window.addEventListener(event, refreshMinutes));
    return () => events.forEach((event) => window.removeEventListener(event, refreshMinutes));
  }, [refreshMinutes]);

  const handleOpenMin = async (m: Minutes, opts?: OpenMinutesOpts) => {
    const request = ++detailRequestRef.current;
    const full = await getMinutes(m.id).catch(() => m);
    if (detailRequestRef.current === request) onOpenMinutes(full, opts);
  };

  const activePipelines = pipelines.filter((p) =>
    p.project_id === project.id &&
    (p.state === "stopping" || p.state === "transcribing"
      || p.state === "done" || p.state === "error")
  );
  const handleDismiss = async (sid: string) => {
    try { await dismissPipeline(sid); } catch {}
    refreshPipelines();
  };

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {debugMode && (
        <DebugPanel
          status={debugStatus}
          streamStatus={streamStatus}
          error={debugError}
        />
      )}

      {/* 録音中でも MinutesList を常に表示(過去議事録を閲覧可能) */}
      <div className="flex-1 overflow-hidden">
        <section className="minutes-page" aria-label="議事録">
          <MinutesList
            minutes={minutes}
            projectId={project.id}
            allProjects={allProjects}
            activeSummarizes={activeSummarizes}
            processing={activePipelines}
            loading={minutesLoading}
            loadingMore={minutesLoadingMore}
            hasMore={minutesHasMore}
            error={minutesError}
            moreError={minutesMoreError}
            onOpenMin={handleOpenMin}
            onOpenPipeline={onOpenPipelineSession}
            onDismissPipeline={handleDismiss}
            onLoadMore={loadMoreMinutes}
            onRetry={loadInitialMinutes}
            onMutated={refreshMinutesAndPipelines}
            onRetryMore={loadMoreMinutes}
          />
        </section>
      </div>
    </div>
  );
}

function DebugPanel({
  status,
  streamStatus,
  error,
}: {
  status: Record<string, any> | null;
  streamStatus: {
    worker_alive?: boolean;
    worker_restarts?: number;
    dropped_chunks?: number;
    last_processed_age_sec?: number | null;
    last_feed_age_sec?: number | null;
  } | null;
  error: string;
}) {
  const process = (status?.process ?? {}) as Record<string, any>;
  const system = (status?.system ?? {}) as Record<string, any>;
  const runtime = (status?.runtime ?? {}) as Record<string, any>;
  const logs = (status?.log_tail ?? []) as string[];
  const states = (runtime.pipelines_by_state ?? {}) as Record<string, number>;
  const stateSummary = Object.entries(states).map(([k, v]) => `${k}:${v}`).join(" ");

  return (
    <div className="px-4 py-2 border-b border-(--border) text-[10px] bg-(--surface)">
      <div className="flex items-center gap-3 text-(--t3) whitespace-nowrap overflow-x-auto">
        <span className="text-(--accent)">DEBUG</span>
        <span>proc CPU {process.cpu_percent ?? "-"}%</span>
        <span>proc RSS {process.rss_mb ?? "-"}MB</span>
        <span>sys MEM {system.memory_percent ?? "-"}%</span>
        <span>threads {process.threads ?? "-"}</span>
        <span>worker {streamStatus?.worker_alive ? "alive" : "down"}</span>
        <span>restart {streamStatus?.worker_restarts ?? 0}</span>
        <span>drop {streamStatus?.dropped_chunks ?? 0}</span>
        {streamStatus?.last_processed_age_sec != null && (
          <span>last-ok {streamStatus.last_processed_age_sec}s</span>
        )}
        {streamStatus?.last_feed_age_sec != null && (
          <span>last-feed {streamStatus.last_feed_age_sec}s</span>
        )}
      </div>
      <div className="mt-1 text-(--t4)">
        session: {String(runtime.active_session_id ?? "-")} · pipelines: {runtime.pipelines_total ?? 0}
        {stateSummary ? ` (${stateSummary})` : ""}
      </div>
      {error ? (
        <div className="mt-1 text-(--danger)">{error}</div>
      ) : (
        <pre className="mt-1 max-h-24 overflow-auto rounded border border-(--border) bg-(--bg) p-2 text-[9px] leading-snug text-(--t3)">
          {logs.slice(-30).join("\n") || "log tail is empty"}
        </pre>
      )}
    </div>
  );
}
