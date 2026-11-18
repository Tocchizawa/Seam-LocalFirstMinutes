import {
  createContext, useCallback, useContext, useEffect, useRef, useState,
  type ReactNode,
} from "react";
import { listen } from "@tauri-apps/api/event";
import {
  WS_URL, getRecordingStatus, getSessionSegments, getWhisperModels, setRecordingPaused, startRecording,
  stopRecording, setMicMuted, updateSettings,
  type TranscriptSegment,
  type WhisperDownloadStatus,
} from "./api";
import { showToast } from "./toast";
import { notifyNative } from "./notify";

export interface LiveSegment extends TranscriptSegment {
  session_id?: string;
}

export interface StreamStatus {
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
  last_feed_age_sec?: number | null;
  transcribe_errors?: number;
  filtered_segments?: number;
  /** Whisper モデルのロード状態: "loading" | "ready" | "error" */
  model_state?: string;
  /** ロード中の Whisper モデル名 (例: "medium") */
  model_name?: string;
  /** model load 失敗時のメッセージ */
  model_error?: string | null;
  model_download?: WhisperDownloadStatus | null;
}

interface Ctx {
  recording: boolean;
  recordingStarting: boolean;
  recordingStopping: boolean;
  waitingForModel: boolean;
  transcriptionDeferred: boolean;
  startRecordingSession: (projectId: string) => Promise<void>;
  cancelRecordingStart: () => void;
  stopRecordingSession: () => Promise<void>;
  paused: boolean;
  togglePaused: () => void;
  micMuted: boolean;
  toggleMicMuted: () => void;
  elapsedSec: number;
  level: number;
  activeSessionId: string | null;
  liveSegments: LiveSegment[];
  streamStatus: StreamStatus | null;
  modelLoadLog: string | null;
  modelDownload: WhisperDownloadStatus | null;
  micDevice: number | null;
  setMicDevice: (n: number | null) => void;
  captureSystem: boolean;
  setCaptureSystem: (b: boolean) => void;
}

interface Session {
  id: string;
  phase: "recording" | "stopping";
  paused: boolean;
  muted: boolean;
  transcriptionDeferred: boolean;
  startedAt: number;
}

type Starting = "preparing" | "waiting-for-model" | null;

const RecordingCtx = createContext<Ctx | null>(null);

const MAX_LIVE_SEGMENTS = 1200;

export function useRecording(): Ctx {
  const v = useContext(RecordingCtx);
  if (!v) throw new Error("useRecording must be inside RecordingProvider");
  return v;
}

export function RecordingProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [starting, setStarting] = useState<Starting>(null);
  const [elapsedSec, setElapsedSec] = useState(0);
  const [level, setLevel] = useState(0);
  const [liveSegments, setLiveSegments] = useState<LiveSegment[]>([]);
  const [streamStatus, setStreamStatus] = useState<StreamStatus | null>(null);
  const [micDevice, setMicDevice] = useState<number | null>(null);
  const [captureSystem, setCaptureSystem] = useState(true);
  const [modelLoadLog, setModelLoadLog] = useState<string | null>(null);
  const [modelDownload, setModelDownload] = useState<WhisperDownloadStatus | null>(null);

  const sessionRef = useRef<Session | null>(null);
  const lastReleased = useRef<string | null>(null);
  const revision = useRef(0);
  const startController = useRef<AbortController | null>(null);
  const pendingControls = useRef(new Map<string, string>());

  const apply = useCallback((next: Session | null) => {
    const previous = sessionRef.current;
    revision.current += 1;
    sessionRef.current = next;
    if (previous?.id !== next?.id) {
      if (previous) lastReleased.current = previous.id;
      setLevel(0);
      setStreamStatus(null);
      setModelDownload(null);
      if (next) setLiveSegments([]);
    }
    setSession(next);
  }, []);

  const syncStatus = useCallback(async () => {
    const before = revision.current;
    try {
      const status = await getRecordingStatus();
      if (before !== revision.current) return;
      if (status.session_id && (status.recording || status.stopping)) {
        apply({
          id: status.session_id,
          phase: status.stopping ? "stopping" : "recording",
          paused: Boolean(status.paused),
          muted: Boolean(status.mic_muted),
          transcriptionDeferred: Boolean(status.transcription_deferred),
          startedAt: performance.now() - (status.elapsed_sec || 0) * 1000,
        });
      } else {
        apply(null);
      }
    } catch {
      // Reconnect and the stop watchdog will retry.
    }
  }, [apply]);

  const startRecordingSession = async (projectId: string) => {
    if (sessionRef.current || startController.current) return;
    const controller = new AbortController();
    startController.current = controller;
    setModelLoadLog(null);
    setStarting("preparing");
    try {
      const started = await startRecording(projectId, micDevice, captureSystem, {
        signal: controller.signal,
        onWaitingForModel: () => setStarting("waiting-for-model"),
      });
      if (!sessionRef.current && lastReleased.current !== started.session_id) {
        apply({
          id: started.session_id,
          phase: "recording",
          paused: false,
          muted: false,
          transcriptionDeferred: Boolean(started.transcription_deferred),
          startedAt: performance.now(),
        });
      }
      void updateSettings({
        recording: {
          last_project_id: projectId,
          last_mic_device: micDevice,
          last_capture_system: captureSystem,
        },
      }).catch(() => {});
    } catch (error) {
      void syncStatus();
      if (!controller.signal.aborted) throw error;
    } finally {
      startController.current = null;
      setStarting(null);
    }
  };

  const cancelRecordingStart = () => startController.current?.abort();

  const stopRecordingSession = async () => {
    const current = sessionRef.current;
    if (!current || current.phase === "stopping") return;
    apply({ ...current, phase: "stopping" });
    try {
      await stopRecording();
    } catch (error) {
      if (sessionRef.current?.id === current.id) {
        apply({ ...sessionRef.current, phase: "recording" });
        await syncStatus();
      }
      throw error;
    }
  };

  const changeControl = async (key: "paused" | "muted") => {
    const current = sessionRef.current;
    if (!current || current.phase !== "recording" || pendingControls.current.get(key) === current.id) return;
    pendingControls.current.set(key, current.id);
    const next = !current[key];
    try {
      if (key === "paused") await setRecordingPaused(next);
      else await setMicMuted(next);
      if (sessionRef.current?.id === current.id) apply({ ...sessionRef.current, [key]: next });
    } catch (error) {
      showToast({ kind: "err", text: error instanceof Error ? error.message : "録音設定を変更できませんでした" });
    } finally {
      if (pendingControls.current.get(key) === current.id) pendingControls.current.delete(key);
    }
  };

  useEffect(() => {
    let ws: WebSocket | null = null;
    let timer = 0;
    let attempt = 0;
    let disposed = false;

    const handle = (event: MessageEvent) => {
      let message: { type?: string; data?: any };
      try { message = JSON.parse(event.data); } catch { return; }
      const { type, data } = message;
      if (disposed || !type) return;
      const current = sessionRef.current;
      const sid: string | undefined = data?.session_id;
      const matches = Boolean(current && (!sid || sid === current.id));

      switch (type) {
        case "recording_status":
          if (sid && sid !== lastReleased.current && (!current || matches)) {
            apply({
              id: sid,
              phase: current?.phase ?? "recording",
              paused: data.paused ?? current?.paused ?? false,
              muted: current?.muted ?? false,
              transcriptionDeferred: data.transcription_deferred ?? current?.transcriptionDeferred ?? false,
              startedAt: typeof data.elapsed_sec === "number"
                ? performance.now() - data.elapsed_sec * 1000
                : current?.startedAt ?? performance.now(),
            });
          }
          return;
        case "recording_stopped":
          if (sid && matches) apply(null);
          else if (!sid) void syncStatus();
          break;
        case "pipeline_progress":
          if (sid && matches && data.state === "transcribing") apply(null);
          if (sid && matches && data.state === "error") void syncStatus();
          break;
        case "pipeline_error":
          if (sid && matches) void syncStatus();
          break;
        case "audio_level":
          if (matches && typeof data?.mic === "number") setLevel(data.mic);
          return;
        case "streaming_status":
          if (matches && data) {
            setStreamStatus(data);
            if (data.model_download) setModelDownload(data.model_download);
          }
          return;
        case "mic_mute_changed":
          if (current && matches && typeof data?.muted === "boolean") apply({ ...current, muted: data.muted });
          return;
        case "transcript_chunk":
          if (matches && data) setLiveSegments((segments) => [...segments.slice(-(MAX_LIVE_SEGMENTS - 1)), data]);
          break;
        case "speaker_renamed":
          if (data?.speaker_id && data.speaker_label) {
            setLiveSegments((segments) => segments.map((segment) =>
              segment.speaker_id === data.speaker_id ? { ...segment, speaker_label: data.speaker_label } : segment,
            ));
          }
          break;
        case "recording_idle_reminder":
          if (!matches) return;
          const minutes = Math.max(1, Math.floor(Number(data?.threshold_sec ?? 300) / 60));
          const notice = `無音が${minutes}分続いています`;
          showToast({ kind: "info", text: notice, ttl: 7000 });
          if (document.visibilityState === "hidden") void notifyNative("録音中", notice);
          return;
        case "mic_silent_warning":
          if (!matches) return;
          const warning = "音声を検出できません。入力デバイスを確認してください。";
          void notifyNative("録音中", warning).then((ok) => {
            if (!ok) showToast({ kind: "err", text: warning, ttl: 12000 });
          });
          return;
      }
      window.dispatchEvent(new CustomEvent(`recording-ws:${type}`, { detail: data }));
    };

    const reconnect = () => {
      if (disposed || timer) return;
      const delay = Math.min(8000, 500 * 1.6 ** Math.min(++attempt, 6));
      timer = window.setTimeout(() => { timer = 0; connect(); }, delay);
    };

    function connect() {
      if (disposed) return;
      try { ws = new WebSocket(WS_URL); }
      catch { reconnect(); return; }
      ws.onopen = () => {
        attempt = 0;
        void syncStatus().then(() => {
          if (disposed) return;
          window.dispatchEvent(new CustomEvent("recording-ws:__open"));
          const id = sessionRef.current?.id;
          if (!id || sessionRef.current?.transcriptionDeferred) return;
          void getSessionSegments(id).then((saved) => {
            if (disposed || sessionRef.current?.id !== id || !Array.isArray(saved)) return;
            setLiveSegments((received) => {
              const byStart = new Map(saved.map((segment) => [segment.start, { ...segment, session_id: id }]));
              for (const segment of received) byStart.set(segment.start, { ...segment, session_id: id });
              return [...byStart.values()].sort((a, b) => a.start - b.start).slice(-MAX_LIVE_SEGMENTS);
            });
          }).catch(() => {});
        });
      };
      ws.onmessage = handle;
      ws.onclose = reconnect;
      ws.onerror = () => ws?.close();
    }

    connect();
    return () => {
      disposed = true;
      window.clearTimeout(timer);
      ws?.close();
    };
  }, [apply, syncStatus]);

  const recording = session !== null;
  const recordingStopping = session?.phase === "stopping";
  const recordingStarting = starting !== null;

  useEffect(() => {
    if (!recordingStopping) return;
    const timer = window.setInterval(syncStatus, 1000);
    return () => window.clearInterval(timer);
  }, [recordingStopping, syncStatus]);

  useEffect(() => {
    if (!recording) return;
    const tick = () => {
      const current = sessionRef.current;
      if (current) setElapsedSec(Math.max(0, Math.floor((performance.now() - current.startedAt) / 1000)));
    };
    tick();
    const timer = window.setInterval(tick, 250);
    return () => window.clearInterval(timer);
  }, [recording, session?.id]);

  useEffect(() => {
    if ((!recording && !recordingStarting) || session?.transcriptionDeferred) return;
    let disposed = false;
    let timer = 0;
    const refresh = async () => {
      try {
        const catalog = await getWhisperModels();
        if (!disposed) setModelDownload(catalog.download);
      } catch {
        // The next read will retry.
      }
      if (!disposed) timer = window.setTimeout(refresh, 1000);
    };
    void refresh();
    return () => {
      disposed = true;
      window.clearTimeout(timer);
    };
  }, [recording, recordingStarting, session?.id, session?.transcriptionDeferred]);

  useEffect(() => {
    const stop = listen<string>("backend-log", (event) => {
      const log = (event.payload || "").trim();
      if (/%\||\b(MB|GB|kB|B)\/s\b|\b(Downloading|Fetching|Loading|Resolving)\b|mlx-whisper|whisper.*model|huggingface_hub/i.test(log)) {
        setModelLoadLog(log);
      }
    });
    return () => { void stop.then((unlisten) => unlisten()).catch(() => {}); };
  }, []);

  return (
    <RecordingCtx.Provider value={{
      recording, recordingStarting, recordingStopping, waitingForModel: starting === "waiting-for-model",
      startRecordingSession, cancelRecordingStart, stopRecordingSession,
      transcriptionDeferred: session?.transcriptionDeferred ?? false,
      paused: session?.paused ?? false,
      togglePaused: () => { void changeControl("paused"); },
      micMuted: session?.muted ?? false,
      toggleMicMuted: () => { void changeControl("muted"); },
      elapsedSec: recording ? elapsedSec : 0,
      level, activeSessionId: session?.id ?? null, liveSegments, streamStatus, modelLoadLog,
      modelDownload, micDevice, setMicDevice, captureSystem, setCaptureSystem,
    }}>
      {children}
    </RecordingCtx.Provider>
  );
}
