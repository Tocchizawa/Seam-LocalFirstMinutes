import { useState, useEffect, useCallback } from "react";
import type { Project, Minutes, PipelineStatus } from "../lib/api";
import type { OpenMinutesOpts } from "../components/MinutesList";
import {
  listMinutes, getMinutes, getPipelines, dismissPipeline, listDevices,
  getSettings, getDebugStatus,
} from "../lib/api";
import { RecordToolbar } from "../components/RecordToolbar";
import { MinutesList } from "../components/MinutesList";
import { DeviceSettingsModal } from "../components/DeviceSettingsModal";
import { LatestSegmentPreview } from "../components/LatestSegmentPreview";
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
  const [pipelines, setPipelines] = useState<PipelineStatus[]>([]);
  const [showDevices, setShowDevices] = useState(false);
  const [debugMode, setDebugMode] = useState(false);
  const [debugStatus, setDebugStatus] = useState<Record<string, any> | null>(null);
  const [debugError, setDebugError] = useState("");

  // 起動時に device 設定を復元
  useEffect(() => {
    Promise.all([listDevices(), getSettings().catch(() => null)]).then(([r, s]) => {
      const rec = (s?.recording as any) || {};
      const savedCapture = typeof rec.last_capture_system === "boolean"
        ? rec.last_capture_system : null;
      if (savedCapture !== null) {
        setCaptureSystem(savedCapture && r.screen_capture_available);
      } else if (r.screen_capture_available) {
        setCaptureSystem(true);
      }
      const savedMic = typeof rec.last_mic_device === "number" ? rec.last_mic_device : null;
      if (savedMic !== null && r.devices.some((d) => d.id === savedMic)) {
        setMicDevice(savedMic);
        return;
      }
      const mic = r.devices.find((d) => d.is_default && !d.is_blackhole);
      if (mic && micDevice === null) setMicDevice(mic.id);
    }).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  const refreshMinutes = useCallback(async () => {
    try { setMinutes(await listMinutes(project.id)); }
    catch { setMinutes([]); }
  }, [project.id]);

  const refreshPipelines = useCallback(async () => {
    try { setPipelines(await getPipelines()); }
    catch { setPipelines([]); }
  }, []);

  useEffect(() => { refreshMinutes(); }, [refreshMinutes]);
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
    const onOpen = () => { refreshPipelines(); refreshMinutes(); };
    window.addEventListener("recording-ws:pipeline_progress", onPipeline);
    window.addEventListener("recording-ws:pipeline_error", onPipeline);
    window.addEventListener("recording-ws:recording_stopped", onPipeline);
    window.addEventListener("recording-ws:pipeline_done", onDone);
    window.addEventListener("recording-ws:__open", onOpen);
    return () => {
      window.removeEventListener("recording-ws:pipeline_progress", onPipeline);
      window.removeEventListener("recording-ws:pipeline_error", onPipeline);
      window.removeEventListener("recording-ws:recording_stopped", onPipeline);
      window.removeEventListener("recording-ws:pipeline_done", onDone);
      window.removeEventListener("recording-ws:__open", onOpen);
    };
  }, [refreshPipelines, refreshMinutes]);

  const handleOpenMin = async (m: Minutes, opts?: OpenMinutesOpts) => {
    try { onOpenMinutes(await getMinutes(m.id), opts); }
    catch { onOpenMinutes(m, opts); }
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

  const handleDismiss = async (sid: string) => {
    try { await dismissPipeline(sid); } catch {}
    refreshPipelines();
  };

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <RecordToolbar
        project={project}
        onStopped={() => { refreshMinutes(); refreshPipelines(); }}
        onOpenDeviceSettings={() => setShowDevices(true)}
      />

      {recording && streamStatus && (
        <StreamStatusBar status={streamStatus} />
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
          <MinutesList
            minutes={minutes}
            projectId={project.id}
            allProjects={allProjects}
            activeSummarizes={activeSummarizes}
            processing={activePipelines}
            onOpenMin={handleOpenMin}
            onOpenPipeline={onOpenPipelineSession}
            onDismissPipeline={handleDismiss}
            onMutated={refreshMinutes}
          />
        </div>
      </div>

      {showDevices && (
        <DeviceSettingsModal
          micDevice={micDevice}
          captureSystem={captureSystem}
          onChangeMicDevice={setMicDevice}
          onChangeCaptureSystem={setCaptureSystem}
          onClose={() => setShowDevices(false)}
        />
      )}
    </div>
  );
}

function StreamStatusBar({ status }: {
  status: {
    queue_size: number;
    queue_maxsize?: number;
    total_segments: number;
    current_chunk_audio_sec?: number;
    current_processing_sec?: number;
    avg_speed_ratio?: number;
    pending_audio_sec?: number;
    worker_alive?: boolean;
    worker_restarts?: number;
    dropped_chunks?: number;
    dropped_audio_sec?: number;
    last_processed_age_sec?: number | null;
    transcribe_errors?: number;
    filtered_segments?: number;
  };
}) {
  const isProcessing = (status.current_chunk_audio_sec ?? 0) > 0;
  const speed = status.avg_speed_ratio ?? 0;
  const pending = status.pending_audio_sec ?? 0;
  const cur = status.current_chunk_audio_sec ?? 0;
  const proc = status.current_processing_sec ?? 0;

  return (
    <div className="flex items-center gap-4 px-4 py-1.5 border-b border-(--border) text-[10px] text-(--t3) num">
      {isProcessing ? (
        <span className="flex items-center gap-1.5 text-(--accent)">
          <span className="status-dot status-dot--pulse" />
          {cur.toFixed(1)}秒の音声を処理中 · {proc.toFixed(1)}秒経過
        </span>
      ) : (
        <span className="text-(--t4)">待機中</span>
      )}
      <span className="ml-auto flex items-center gap-3">
        {speed > 0 && (
          <span title="直近の処理速度(>1 で実時間より速い)">
            速度 ×{speed.toFixed(1)}
          </span>
        )}
        {pending > 0 && (
          <span title="未処理の音声合計">
            未処理 {pending.toFixed(1)}秒
          </span>
        )}
        <span title="キューサイズ">
          待機中 {status.queue_size}{status.queue_maxsize ? `/${status.queue_maxsize}` : ""}
        </span>
        {status.dropped_chunks ? (
          <span className="text-(--warning)" title="過負荷で破棄した音声">
            drop {status.dropped_chunks}件 ({(status.dropped_audio_sec ?? 0).toFixed(1)}秒)
          </span>
        ) : null}
        {status.transcribe_errors ? (
          <span className="text-(--danger)" title="文字起こし失敗回数">
            err {status.transcribe_errors}
          </span>
        ) : null}
        {status.filtered_segments ? (
          <span className="text-(--t3)" title="ハルシネーションとして除外した件数">
            filter {status.filtered_segments}
          </span>
        ) : null}
        <span>確定 {status.total_segments}件</span>
      </span>
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
