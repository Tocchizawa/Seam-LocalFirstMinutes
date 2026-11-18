import { useState, useEffect, useCallback, useRef } from "react";
import type {
  Project, Minutes, PipelineStatus, RecoveryStatus, RecoveryItem,
} from "../lib/api";
import type { OpenMinutesOpts } from "../components/MinutesList";
import {
  listMinutes, getMinutes, getPipelines, dismissPipeline,
  getSettings, getDebugStatus, getRecoveryStatus,
} from "../lib/api";
import { RecordToolbar } from "../components/RecordToolbar";
import { StreamStatusBar } from "../components/StreamStatusBar";
import { MinutesList } from "../components/MinutesList";
import { DeviceSettingsModal } from "../components/DeviceSettingsModal";
import { LatestSegmentPreview } from "../components/LatestSegmentPreview";
import { Spinner } from "../components/Spinner";
import { CheckCircle, X } from "@phosphor-icons/react";
import { useRecording } from "../lib/recording-context";

interface Props {
  project: Project;
  allProjects: Project[];
  /** 進行中の要約ジョブ: minutes_id → state */
  activeSummarizes: Map<string, string>;
  onOpenMinutes: (m: Minutes, opts?: OpenMinutesOpts) => void;
  onOpenPipelineSession: (sessionId: string) => void;
  onOpenLive: () => void;
}

const MINUTES_PAGE_SIZE = 20;

export function MainView({
  project, allProjects, activeSummarizes,
  onOpenMinutes, onOpenPipelineSession, onOpenLive,
}: Props) {
  const {
    recording, streamStatus,
    setActiveSessionId, micDevice, setMicDevice,
    captureSystem, setCaptureSystem,
  } = useRecording();

  const [minutes, setMinutes] = useState<Minutes[]>([]);
  const [minutesLoading, setMinutesLoading] = useState(false);
  const [minutesLoadingMore, setMinutesLoadingMore] = useState(false);
  const [minutesHasMore, setMinutesHasMore] = useState(false);
  const [minutesError, setMinutesError] = useState("");
  const [minutesMoreError, setMinutesMoreError] = useState("");
  const loadingMoreRef = useRef(false);
  const projectIdRef = useRef(project.id);
  const minutesRequestRef = useRef(0);
  const pipelineRequestRef = useRef(0);
  projectIdRef.current = project.id;
  const [pipelines, setPipelines] = useState<PipelineStatus[]>([]);
  const [recovery, setRecovery] = useState<RecoveryStatus | null>(null);
  const [recoveryDismissed, setRecoveryDismissed] = useState(false);
  const [showDevices, setShowDevices] = useState(false);
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

  const refreshPipelines = useCallback(async () => {
    const requestId = ++pipelineRequestRef.current;
    try {
      const result = await getPipelines();
      if (requestId === pipelineRequestRef.current) setPipelines(result);
    }
    catch { /* Keep the last known pipeline state during a temporary API outage. */ }
  }, []);

  const refreshMinutesAndPipelines = useCallback(() => {
    refreshMinutes();
    refreshPipelines();
  }, [refreshMinutes, refreshPipelines]);

  useEffect(() => { refreshPipelines(); }, [refreshPipelines]);

  useEffect(() => {
    const onUpdated = () => refreshMinutes();
    window.addEventListener("minutes-updated", onUpdated as EventListener);
    return () => window.removeEventListener("minutes-updated", onUpdated as EventListener);
  }, [refreshMinutes]);

  // RecordingProvider が再配信する WS イベントを listen
  useEffect(() => {
    const onPipeline = () => { refreshPipelines(); };
    const onDone = () => { refreshPipelines(); refreshMinutes(); };
    const onOpen = () => {
      refreshPipelines();
      refreshMinutes();
      getRecoveryStatus().then(setRecovery).catch(() => {});
    };
    const onRecoveryStarted = (ev: Event) => {
      const detail = (ev as CustomEvent<{ total: number }>).detail;
      setRecovery({
        state: "running",
        total: detail?.total ?? 0,
        current: 0,
        item: null,
        recovered: 0,
        finished_at: null,
      });
      setRecoveryDismissed(false);
    };
    const onRecoveryProgress = (ev: Event) => {
      const detail = (ev as CustomEvent<{
        current: number; total: number; item: RecoveryItem | null;
      }>).detail;
      setRecovery((prev) => ({
        state: "running",
        total: detail?.total ?? prev?.total ?? 0,
        current: detail?.current ?? 0,
        item: detail?.item ?? null,
        recovered: prev?.recovered ?? 0,
        finished_at: null,
      }));
    };
    const onRecoveryFinished = (ev: Event) => {
      const detail = (ev as CustomEvent<{ total: number; recovered: number }>).detail;
      setRecovery({
        state: "done",
        total: detail?.total ?? 0,
        current: detail?.total ?? 0,
        item: null,
        recovered: detail?.recovered ?? 0,
        finished_at: new Date().toISOString(),
      });
      refreshMinutes();
    };
    // 要約完了 / タイトル自動生成 / 話者改名なども一覧に反映する
    const onMinutesMutated = () => refreshMinutes();
    window.addEventListener("recording-ws:pipeline_progress", onPipeline);
    window.addEventListener("recording-ws:pipeline_error", onPipeline);
    window.addEventListener("recording-ws:recording_stopped", onPipeline);
    window.addEventListener("recording-ws:pipeline_done", onDone);
    window.addEventListener("recording-ws:__open", onOpen);
    window.addEventListener("recording-ws:session_recovery_started", onRecoveryStarted);
    window.addEventListener("recording-ws:session_recovery_progress", onRecoveryProgress);
    window.addEventListener("recording-ws:session_recovery_finished", onRecoveryFinished);
    window.addEventListener("recording-ws:summary_done", onMinutesMutated);
    window.addEventListener("recording-ws:summary_failed", onMinutesMutated);
    window.addEventListener("recording-ws:summary_cancelled", onMinutesMutated);
    window.addEventListener("recording-ws:summary_skipped", onMinutesMutated);
    window.addEventListener("recording-ws:minutes_title_updated", onMinutesMutated);
    return () => {
      window.removeEventListener("recording-ws:pipeline_progress", onPipeline);
      window.removeEventListener("recording-ws:pipeline_error", onPipeline);
      window.removeEventListener("recording-ws:recording_stopped", onPipeline);
      window.removeEventListener("recording-ws:pipeline_done", onDone);
      window.removeEventListener("recording-ws:__open", onOpen);
      window.removeEventListener("recording-ws:session_recovery_started", onRecoveryStarted);
      window.removeEventListener("recording-ws:session_recovery_progress", onRecoveryProgress);
      window.removeEventListener("recording-ws:session_recovery_finished", onRecoveryFinished);
      window.removeEventListener("recording-ws:summary_done", onMinutesMutated);
      window.removeEventListener("recording-ws:summary_failed", onMinutesMutated);
      window.removeEventListener("recording-ws:summary_cancelled", onMinutesMutated);
      window.removeEventListener("recording-ws:summary_skipped", onMinutesMutated);
      window.removeEventListener("recording-ws:minutes_title_updated", onMinutesMutated);
    };
  }, [refreshPipelines, refreshMinutes]);

  // 起動時の復元状態を最初に一度同期 (WS 接続前に終わっていた場合の保険)
  useEffect(() => {
    getRecoveryStatus().then(setRecovery).catch(() => {});
  }, []);

  // done 状態は数秒で自動的に閉じる
  useEffect(() => {
    if (recovery?.state !== "done") return;
    const t = window.setTimeout(() => setRecoveryDismissed(true), 6000);
    return () => window.clearTimeout(t);
  }, [recovery?.state, recovery?.finished_at]);

  const handleOpenMin = async (m: Minutes, opts?: OpenMinutesOpts) => {
    const requestedProjectId = project.id;
    try {
      const full = await getMinutes(m.id);
      if (projectIdRef.current === requestedProjectId) onOpenMinutes(full, opts);
    } catch {
      if (projectIdRef.current === requestedProjectId) onOpenMinutes(m, opts);
    }
  };

  // 現在録音中の session_id を pipelines から特定して context へ
  useEffect(() => {
    const rec = pipelines.find((p) => p.state === "recording");
    setActiveSessionId(rec?.session_id ?? null);
  }, [pipelines, setActiveSessionId]);

  const activePipelines = pipelines.filter((p) =>
    p.project_id === project.id &&
    (p.state === "stopping" || p.state === "transcribing"
      || p.state === "done" || p.state === "error")
  );
  const activePipelineCount = activePipelines.filter((p) =>
    p.state === "stopping" || p.state === "transcribing"
  ).length;

  const handleDismiss = async (sid: string) => {
    try { await dismissPipeline(sid); } catch {}
    refreshPipelines();
  };

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <RecordToolbar
        project={project}
        onStopped={refreshMinutesAndPipelines}
        onOpenDeviceSettings={() => setShowDevices(true)}
      />

      {recording && streamStatus && (
        <StreamStatusBar status={streamStatus} />
      )}

      {recovery && !recoveryDismissed
        && (recovery.state === "running" || recovery.state === "done")
        && (recovery.total > 0 || recovery.recovered > 0)
        && (
          <RecoveryBanner
            status={recovery}
            onDismiss={() => setRecoveryDismissed(true)}
          />
      )}

      {recording && <LatestSegmentPreview onExpand={onOpenLive} />}
      {debugMode && (
        <DebugPanel
          status={debugStatus}
          streamStatus={streamStatus}
          error={debugError}
        />
      )}

      {/* 録音中でも MinutesList を常に表示(過去議事録を閲覧可能) */}
      <div className="flex-1 overflow-hidden">
        <div className="h-full overflow-y-auto" key={project.id}>
          <header className="minutes-page-header">
            <div className="min-w-0">
              <p className="minutes-page-kicker">{project.name}</p>
              <div className="flex items-baseline gap-3 flex-wrap">
                <h1>議事録</h1>
                <span className="minutes-page-count num">{minutes.length} 件を表示</span>
              </div>
              <p className="minutes-page-description">会議の録音、文字起こし、要約をまとめて管理</p>
            </div>
            {activePipelineCount > 0 && (
              <div className="minutes-page-activity" aria-live="polite">
                <span className="status-dot status-dot--pulse" />
                文字起こし中 {activePipelineCount} 件
              </div>
            )}
          </header>
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
        </div>
      </div>

      {showDevices && (
        <DeviceSettingsModal
          micDevice={micDevice}
          captureSystem={captureSystem}
          refreshOnOpen={!recording}
          onChangeMicDevice={setMicDevice}
          onChangeCaptureSystem={setCaptureSystem}
          onClose={() => setShowDevices(false)}
        />
      )}
    </div>
  );
}


function RecoveryBanner({
  status, onDismiss,
}: {
  status: RecoveryStatus;
  onDismiss: () => void;
}) {
  const isDone = status.state === "done";
  const itemLabel = (() => {
    const it = status.item;
    if (!it) return null;
    const date = it.date || "";
    const md = (() => {
      if (!date) return "";
      const parts = date.split("-");
      if (parts.length !== 3) return date;
      return `${Number(parts[1])}/${Number(parts[2])}`;
    })();
    const hhmm = it.hhmm && it.hhmm !== "??:??" ? it.hhmm : "";
    return [md, hhmm].filter(Boolean).join(" ");
  })();

  return (
    <div className="recovery-banner">
      <div className="recovery-banner-icon">
        {isDone ? (
          <CheckCircle size={14} weight="fill" className="text-(--accent)" />
        ) : (
          <Spinner size={12} color="var(--accent)" />
        )}
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-baseline gap-2">
          <span className="text-[12px] font-medium text-(--t1)">
            {isDone ? "未完了セッションを復元しました" : "未完了セッションを復元中"}
          </span>
          {!isDone && status.total > 0 && (
            <span className="num text-[11px] text-(--t3) tabular-nums">
              {status.current}/{status.total}
            </span>
          )}
          {isDone && (
            <span className="num text-[11px] text-(--t3) tabular-nums">
              {status.recovered}/{status.total} 件
            </span>
          )}
        </div>
        {!isDone && itemLabel && (
          <p className="text-[11px] text-(--t3) truncate mt-0.5">
            復元中: {itemLabel} の会議
          </p>
        )}
      </div>
      {!isDone && status.total > 0 && (
        <div className="recovery-progress">
          <div
            className="recovery-progress-fill"
            style={{
              width: `${Math.max(
                3,
                Math.round((status.current / Math.max(1, status.total)) * 100),
              )}%`,
            }}
          />
        </div>
      )}
      {isDone && (
        <button
          onClick={onDismiss}
          className="icon-btn !w-6 !h-6"
          aria-label="閉じる"
          title="閉じる"
        >
          <X size={11} weight="bold" />
        </button>
      )}
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
