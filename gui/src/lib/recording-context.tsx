import {
  createContext, useCallback, useContext, useEffect, useRef, useState,
  type ReactNode,
} from "react";
import { listen } from "@tauri-apps/api/event";
import {
  WS_URL, getRecordingStatus, type TranscriptSegment,
} from "./api";
import { showToast } from "./toast";
import { isByteSizeToken, parseHFProgress, parseSize } from "./parse-hf-progress";

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
}

interface Ctx {
  recording: boolean;
  setRecording: (b: boolean) => void;
  paused: boolean;
  togglePaused: () => void;
  setPaused: (b: boolean) => void;
  /** マイクのソフトミュート (system audio は無関係) */
  micMuted: boolean;
  setMicMutedLocal: (b: boolean) => void;
  elapsedSec: number;
  resetElapsed: () => void;
  level: number;
  activeSessionId: string | null;
  setActiveSessionId: (s: string | null) => void;
  liveSegments: LiveSegment[];
  resetLive: () => void;
  streamStatus: StreamStatus | null;
  /** モデル DL / 読込進捗の直近ログ行 (HF tqdm 等を抜粋) */
  modelLoadLog: string | null;
  /** 全 DL ファイル合算の進捗 (HF tqdm 行ベース) */
  modelDownloadAggregate: {
    percent: number;
    currentBytes: number;
    totalBytes: number;
    filesCount: number;
  } | null;
  // device preferences shared between toolbar & banner
  micDevice: number | null;
  setMicDevice: (n: number | null) => void;
  captureSystem: boolean;
  setCaptureSystem: (b: boolean) => void;
  syncRecordingStatus: () => Promise<void>;
  resetRecordingState: () => void;
}

const RecordingCtx = createContext<Ctx | null>(null);

const MAX_LIVE_SEGMENTS = 1200;

export function useRecording(): Ctx {
  const v = useContext(RecordingCtx);
  if (!v) throw new Error("useRecording must be inside RecordingProvider");
  return v;
}

export function RecordingProvider({ children }: { children: ReactNode }) {
  const [recording, setRecording] = useState(false);
  const [paused, setPaused] = useState(false);
  const [elapsedSec, setElapsedSec] = useState(0);
  const [level, setLevel] = useState(0);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [liveSegments, setLiveSegments] = useState<LiveSegment[]>([]);
  const [streamStatus, setStreamStatus] = useState<StreamStatus | null>(null);
  const [micDevice, setMicDevice] = useState<number | null>(null);
  const [captureSystem, setCaptureSystem] = useState(false);
  const [modelLoadLog, setModelLoadLog] = useState<string | null>(null);
  const [micMuted, setMicMutedLocal] = useState(false);
  /** filename → { current, total } (バイト) */
  const downloadFilesRef = useRef<Map<string, { current: number; total: number }>>(new Map());
  const [modelDownloadAggregate, setModelDownloadAggregate] = useState<
    { percent: number; currentBytes: number; totalBytes: number; filesCount: number } | null
  >(null);

  const startRef = useRef(0);
  const recordingRef = useRef(recording);
  const activeSessionIdRef = useRef(activeSessionId);
  const pausedRef = useRef(paused);
  useEffect(() => { recordingRef.current = recording; }, [recording]);
  useEffect(() => { activeSessionIdRef.current = activeSessionId; }, [activeSessionId]);
  useEffect(() => { pausedRef.current = paused; }, [paused]);

  const resetRecordingState = useCallback(() => {
    setRecording(false);
    setActiveSessionId(null);
    setElapsedSec(0);
    setLevel(0);
    setPaused(false);
    setMicMutedLocal(false);
    setStreamStatus(null);
  }, []);

  const syncRecordingStatus = useCallback(async () => {
    const s = await getRecordingStatus();
    if (s.recording) {
      setRecording(true);
      setActiveSessionId(s.session_id ?? null);
      setElapsedSec(Math.floor(s.elapsed_sec));
      if (typeof s.mic_muted === "boolean") setMicMutedLocal(s.mic_muted);
    } else {
      resetRecordingState();
    }
  }, [resetRecordingState]);

  // 起動時に既存のセッションを復元
  useEffect(() => {
    syncRecordingStatus().catch(() => {});
  }, [syncRecordingStatus]);

  useEffect(() => {
    const stop = listen<{ phase?: string }>("backend-status", (e) => {
      if (e.payload?.phase === "error") {
        resetRecordingState();
      } else if (e.payload?.phase === "ready") {
        syncRecordingStatus().catch(() => {});
      }
    });
    return () => {
      stop.then((un) => un()).catch(() => { /* noop */ });
    };
  }, [resetRecordingState, syncRecordingStatus]);

  const resetLive = useCallback(() => setLiveSegments([]), []);
  const resetElapsed = useCallback(() => setElapsedSec(0), []);
  const togglePaused = useCallback(() => setPaused((p) => !p), []);

  // 単一 WebSocket: 録音関連イベントは内部 state へ、パイプライン系は CustomEvent で再配信
  useEffect(() => {
    let ws: WebSocket | null = null;
    let timer: number | null = null;
    let attempt = 0;
    let stopped = false;

    const dispatch = (type: string, detail: unknown) => {
      window.dispatchEvent(new CustomEvent(`recording-ws:${type}`, { detail }));
    };

    const handle = (e: MessageEvent) => {
      let m: { type?: string; data?: any };
      try { m = JSON.parse(e.data); } catch { return; }
      const t = m.type;
      if (!t) return;

      if (t === "audio_level") {
        if (typeof m.data?.mic === "number") setLevel(m.data.mic);
        return;
      }
      if (t === "recording_status") {
        if (typeof m.data?.elapsed_sec === "number") {
          const sec = Math.floor(m.data.elapsed_sec);
          setElapsedSec((prev) => (prev !== sec ? sec : prev));
        }
        if (typeof m.data?.session_id === "string") {
          setActiveSessionId(m.data.session_id);
        }
        return;
      }
      if (t === "recording_stopped") {
        const stoppedSession = typeof m.data?.session_id === "string" ? m.data.session_id : null;
        const activeSession = activeSessionIdRef.current;
        if (!stoppedSession || stoppedSession === activeSession || (recordingRef.current && !activeSession)) {
          setRecording(false);
          setActiveSessionId(null);
          setStreamStatus(null);
        }
        dispatch(t, m.data);
        return;
      }
      if (t === "pipeline_error") {
        const failedSession = typeof m.data?.session_id === "string" ? m.data.session_id : null;
        const activeSession = activeSessionIdRef.current;
        if (failedSession && recordingRef.current && (failedSession === activeSession || !activeSession)) {
          setRecording(false);
          setActiveSessionId(null);
          setStreamStatus(null);
        }
        dispatch(t, m.data);
        return;
      }
      if (t === "transcript_chunk" && m.data) {
        const seg: LiveSegment = m.data;
        setLiveSegments((prev) => {
          const next = [...prev, seg];
          if (next.length <= MAX_LIVE_SEGMENTS) return next;
          return next.slice(next.length - MAX_LIVE_SEGMENTS);
        });
        return;
      }
      if (t === "speaker_renamed" && m.data?.speaker_id && m.data?.speaker_label) {
        const speakerId = String(m.data.speaker_id);
        const speakerLabel = String(m.data.speaker_label);
        setLiveSegments((prev) =>
          prev.map((seg) => (
            seg.speaker_id === speakerId ? { ...seg, speaker_label: speakerLabel } : seg
          ))
        );
        // パイプライン側にも届ける必要があれば追加配信
        dispatch(t, m.data);
        return;
      }
      if (t === "streaming_status" && m.data) {
        setStreamStatus(m.data);
        return;
      }
      if (t === "mic_mute_changed" && m.data) {
        if (typeof m.data.muted === "boolean") setMicMutedLocal(m.data.muted);
        return;
      }
      if (t === "recording_idle_reminder") {
        const thresholdSec = Number(m.data?.threshold_sec ?? 300);
        const min = Math.max(1, Math.floor(thresholdSec / 60));
        const text = `無音が${min}分続いています。録音停止忘れにご注意ください。`;
        showToast({ kind: "info", text, ttl: 7000 });
        if (
          typeof window !== "undefined"
          && "Notification" in window
          && document.visibilityState === "hidden"
          && Notification.permission === "granted"
        ) {
          new Notification("Seam", { body: text });
        }
        return;
      }
      if (t === "mic_silent_warning") {
        showToast({
          kind: "err",
          text: "マイクの音声が検出されません。デバイス設定を確認してください。",
          ttl: 12000,
        });
        return;
      }
      // パイプライン関連 / その他は MainView 側に再配信
      dispatch(t, m.data);
    };

    const connect = () => {
      if (stopped) return;
      try { ws = new WebSocket(WS_URL); }
      catch { scheduleReconnect(); return; }
      ws.onopen = () => {
        attempt = 0;
        dispatch("__open", null);
        syncRecordingStatus().catch(() => {});
      };
      ws.onmessage = handle;
      ws.onclose = scheduleReconnect;
      ws.onerror = () => { try { ws?.close(); } catch {} };
    };

    const scheduleReconnect = () => {
      if (stopped || timer != null) return;
      attempt++;
      const delay = Math.min(8000, 500 * Math.pow(1.6, Math.min(attempt, 6)));
      timer = window.setTimeout(() => { timer = null; connect(); }, delay);
    };

    connect();
    return () => {
      stopped = true;
      if (timer) window.clearTimeout(timer);
      try { ws?.close(); } catch {}
    };
  }, [syncRecordingStatus]);

  // elapsed の細かい tick はクライアントで補完(無音時のみ backend からの recording_status が来ない可能性)
  useEffect(() => {
    if (!recording || paused) return;
    startRef.current = Date.now() - elapsedSec * 1000;
    const id = window.setInterval(() => {
      const sec = Math.floor((Date.now() - startRef.current) / 1000);
      setElapsedSec((prev) => (prev !== sec ? sec : prev));
    }, 250);
    return () => window.clearInterval(id);
  }, [recording, paused, elapsedSec]);

  // 録音停止時のクリーンアップ
  useEffect(() => {
    if (recording) return;
    setLevel(0);
    setPaused(false);
    setMicMutedLocal(false);
  }, [recording]);

  // backend-log を購読: モデル DL / ロード関連の行を保持
  useEffect(() => {
    const stop = listen<string>("backend-log", (e) => {
      const text = (e.payload || "").trim();
      if (!text) return;
      // HF Hub の tqdm / mlx-whisper のロードログを拾う
      const isModelLog =
        /%\|/.test(text) ||                            // tqdm bar
        /\b(MB|GB|kB|B)\/s\b/.test(text) ||            // 速度表記
        /\b(Downloading|Fetching|Loading|Resolving)\b/i.test(text) ||
        /mlx-whisper|whisper.*model|huggingface_hub/i.test(text);
      if (!isModelLog) return;

      setModelLoadLog(text);

      // 個別ファイルの DL 進捗を集計用に保持
      const prog = parseHFProgress(text);
      if (prog && prog.current && prog.total) {
        const byteBased = isByteSizeToken(prog.current) && isByteSizeToken(prog.total);
        if (byteBased && prog.filename) {
          const cur = parseSize(prog.current);
          const tot = parseSize(prog.total);
          if (tot > 0) {
            downloadFilesRef.current.set(prog.filename, { current: cur, total: tot });
            let totalBytes = 0;
            let currentBytes = 0;
            for (const v of downloadFilesRef.current.values()) {
              totalBytes += v.total;
              currentBytes += v.current;
            }
            setModelDownloadAggregate({
              percent: totalBytes > 0 ? (currentBytes / totalBytes) * 100 : 0,
              currentBytes,
              totalBytes,
              filesCount: downloadFilesRef.current.size,
            });
          }
          return;
        }

        // "Fetching 4 files: 50%|...| 2/4 [...]" のような件数ベース進捗。
        // バイト総量は不明なので % のみ UI に反映する。
        const curNum = Number.parseFloat(prog.current);
        const totNum = Number.parseFloat(prog.total);
        if (Number.isFinite(curNum) && Number.isFinite(totNum) && totNum > 0) {
          const percent = Number.isFinite(prog.percent) ? prog.percent : ((curNum / totNum) * 100);
          setModelDownloadAggregate({
            percent: Math.max(0, Math.min(100, percent)),
            currentBytes: 0,
            totalBytes: 0,
            filesCount: Math.round(totNum),
          });
        }
      }
    });
    return () => {
      stop.then((un) => un()).catch(() => { /* noop */ });
    };
  }, []);

  const value: Ctx = {
    recording, setRecording,
    paused, togglePaused, setPaused,
    micMuted, setMicMutedLocal,
    elapsedSec, resetElapsed,
    level,
    activeSessionId, setActiveSessionId,
    liveSegments, resetLive,
    streamStatus,
    modelLoadLog,
    modelDownloadAggregate,
    micDevice, setMicDevice,
    captureSystem, setCaptureSystem,
    syncRecordingStatus,
    resetRecordingState,
  };

  return <RecordingCtx.Provider value={value}>{children}</RecordingCtx.Provider>;
}
