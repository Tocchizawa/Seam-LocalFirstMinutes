import { useState, useRef, useEffect, useMemo, useCallback } from "react";
import {
  ArrowLeft, Copy, DownloadSimple, Trash, Play, Pause, ArrowsClockwise, PencilSimple,
  CaretDown, CaretUp, MagnifyingGlass, X, MagicWand, WarningCircle, Stop,
} from "@phosphor-icons/react";
import type {
  Minutes, TranscriptSegment, PipelineStatus,
  SummarizeStatus, SummarizeProvider,
} from "../lib/api";
import {
  audioPlayUrl, exportMinutes, getMinutesMarkdown, deleteMinutes,
  getPipelineStatus, getMinutes, retranscribeMinutes, cancelRetranscribeMinutes, updateMinutesTitle, WS_URL,
  triggerSummarize, getSummarizeStatus, cancelSummarize, getPipelineBySession,
  updateMinutesSummary, getSessionSegments, getSessionAudioInfo, recoverSessionMinutes,
  PROVIDER_LABELS, SUMMARIZE_PROVIDERS, CLOUD_PROVIDERS,
} from "../lib/api";
import { Spinner } from "../components/Spinner";
import { Waveform } from "../components/Waveform";
import { MarkdownEditor } from "../components/MarkdownEditor";
import { MarkdownView } from "../components/MarkdownView";
import { Select } from "../components/Select";
import { useImeSafeEnter } from "../lib/ime";
import { showToast } from "../lib/toast";
import { ask } from "@tauri-apps/plugin-dialog";
import { writeClipboard, openInDefaultApp } from "../lib/api";

interface CommonProps {
  onBack: () => void;
  onDeleted?: () => void;
}
interface MinutesProps extends CommonProps {
  minutes: Minutes;
  sessionId?: undefined;
  initialQuery?: string;
  initialTab?: "summary" | "transcript";
}
interface SessionProps extends CommonProps {
  sessionId: string;
  minutes?: undefined;
  initialQuery?: undefined;
  initialTab?: undefined;
}
type Props = MinutesProps | SessionProps;
interface LiveSegment extends TranscriptSegment {
  session_id?: string;
}

const MAX_LIVE_SEGMENTS = 1200;

function fmt(s: number) {
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}
function fmtDuration(s: number) {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h}時間${m}分` : `${m}分`;
}

function fmtBytes(n: number): string {
  if (!n || n < 0) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export function DetailView(props: Props) {
  const isLive = !!props.sessionId;
  const [pipeline, setPipeline] = useState<PipelineStatus | null>(null);
  const [liveSegments, setLiveSegments] = useState<TranscriptSegment[]>([]);
  const [currentMinutes, setCurrentMinutes] = useState<Minutes | undefined>(props.minutes);
  // props.minutes が変わったら state も同期(別の議事録を開いた場合)
  useEffect(() => { setCurrentMinutes(props.minutes); }, [props.minutes]);

  useEffect(() => {
    if (!isLive) return;
    const fetchPipeline = () => getPipelineStatus().then(setPipeline).catch(() => {});
    fetchPipeline();

    let ws: WebSocket | null = null;
    let timer: number | null = null;
    let attempt = 0;
    let stopped = false;

    const handle = (e: MessageEvent) => {
      try {
        const m = JSON.parse(e.data);
        if (m.type === "transcript_chunk" && m.data) {
          const seg = m.data as LiveSegment;
          if (props.sessionId && seg.session_id !== props.sessionId) return;
          setLiveSegments((prev) => {
            // start 時刻が一致する既存セグメントは置換、無ければ追加してソート
            const filtered = prev.filter((p) => p.start !== seg.start);
            const next = [...filtered, seg].sort((a, b) => a.start - b.start);
            if (next.length <= MAX_LIVE_SEGMENTS) return next;
            return next.slice(next.length - MAX_LIVE_SEGMENTS);
          });
        }
        if (m.type === "speaker_renamed" && m.data?.speaker_id && m.data?.speaker_label) {
          const speakerId = String(m.data.speaker_id);
          const speakerLabel = String(m.data.speaker_label);
          setLiveSegments((prev) =>
            prev.map((seg) =>
              seg.speaker_id === speakerId
                ? { ...seg, speaker_label: speakerLabel }
                : seg
            )
          );
        }
        if (["pipeline_progress", "pipeline_done", "pipeline_error", "recording_stopped"].includes(m.type)) {
          fetchPipeline();
        }
        // pipeline-detail モードでこの session が finalize されたら、
        // バックエンドが作成した minutes を fetch して state を切替える。
        // これがないと currentMinutes が undefined のままになり、
        // canRecoverSession (= !minutesId && stateLive==="done") が誤って真になり
        // 「救済ボタン」が表示される。
        if (
          m.type === "pipeline_done"
          && m.data?.session_id === props.sessionId
          && typeof m.data?.minutes_id === "string"
        ) {
          const newId = m.data.minutes_id as string;
          getMinutes(newId)
            .then((fresh) => setCurrentMinutes(fresh))
            .catch(() => {});
        }
      } catch {}
    };

    const connect = () => {
      if (stopped) return;
      try { ws = new WebSocket(WS_URL); } catch { scheduleReconnect(); return; }
      ws.onopen = () => { attempt = 0; fetchPipeline(); };
      ws.onmessage = handle;
      ws.onclose = scheduleReconnect;
      ws.onerror = () => { try { ws?.close(); } catch {} };
    };
    const scheduleReconnect = () => {
      if (stopped || timer) return;
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
  }, [isLive, props.sessionId]);

  // 録音中 / 録音直後に DetailView を開いた際、それまでに確定済みの segment を
  // バックエンド (transcript.jsonl) から取得して seed する。
  // これがないと WS で「これから来る分」しか見えず、過去の発話が欠落する。
  useEffect(() => {
    if (!isLive || !props.sessionId) return;
    let cancelled = false;
    (async () => {
      try {
        const segs = await getSessionSegments(props.sessionId);
        if (cancelled || !Array.isArray(segs) || segs.length === 0) return;
        setLiveSegments((prev) => {
          const byStart = new Map<number, TranscriptSegment>();
          // 既存 (WS で先に来た新規) を優先して残しつつ、不足を埋める
          for (const s of segs) byStart.set(s.start, s);
          for (const p of prev) byStart.set(p.start, p);
          const merged = Array.from(byStart.values())
            .sort((a, b) => a.start - b.start);
          if (merged.length <= MAX_LIVE_SEGMENTS) return merged;
          return merged.slice(merged.length - MAX_LIVE_SEGMENTS);
        });
      } catch { /* セッションが古い等は無視 */ }
    })();
    return () => { cancelled = true; };
  }, [isLive, props.sessionId]);

  const liveResult = pipeline?.transcript;
  const minutes = currentMinutes;
  const sessionId = props.sessionId ?? minutes?.session_id ?? "";
  const duration = minutes?.duration_sec ?? pipeline?.result?.duration_sec ?? 0;
  // 録音停止 pipeline 中はバックエンドから送られた transcript_chunk が
  // liveSegments に蓄積されているのでそれを優先表示。
  // pipeline 完了後は pipeline.transcript / minutes.transcript が確定する。
  const transcript: TranscriptSegment[] = (() => {
    if (minutes?.transcript) return minutes.transcript;
    const segs = liveResult?.segments;
    if (segs && segs.length > 0) return segs;
    return liveSegments;
  })();
  const summary = minutes?.summary;
  // 録音中 / finalize 中の場合、DB に minutes 行がまだ無いので sessionId
  // (YYYYMMDD_HHMMSS) からタイトルと日付を派生させる。
  // 表記は finalize 時に backend が生成するタイトル ("HH:MM の会議") に揃える。
  const derivedFromSession = (() => {
    if (!isLive || !sessionId) return null;
    const m = sessionId.match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})/);
    if (!m) return null;
    return {
      title: `${m[4]}:${m[5]} の会議`,
      date: `${m[1]}-${m[2]}-${m[3]}`,
    };
  })();
  const title = minutes?.title ?? derivedFromSession?.title ?? "録音セッション";
  const date = minutes?.date ?? derivedFromSession?.date;
  const minutesId = minutes?.id;
  const stateLive = isLive ? (pipeline?.state ?? "idle") : "done";
  const liveMessage = pipeline?.message ?? "";
  const liveError = pipeline?.error ?? null;

  /* 再文字起こし状態 (非-live のみ) */
  const [retryStatus, setRetryStatus] = useState<{
    state: "queued" | "transcribing" | "stopping" | "error";
    message: string;
    progress: number; // 0..1
    stage?: string | null;
    stage_label?: string | null;
    stage_step?: number | null;
    stage_total?: number | null;
  } | null>(null);

  /* mount/再表示時に再文字起こしの状態を復元 (画面遷移してもステータスを失わないため) */
  useEffect(() => {
    if (isLive || !sessionId) return;
    let cancelled = false;
    (async () => {
      try {
        const p = await getPipelineBySession(sessionId);
        if (cancelled || !p) return;
        // in-flight (transcribing/stopping) のみ復元。done/error は表示不要。
        if (p.state === "transcribing" || p.state === "stopping") {
          setRetryStatus({
            state: p.state as "transcribing" | "stopping",
            message: p.message || "処理中...",
            progress: typeof p.progress === "number" ? p.progress : 0,
            stage: p.stage ?? null,
            stage_label: p.stage_label ?? null,
            stage_step: typeof p.stage_step === "number" ? p.stage_step : null,
            stage_total: typeof p.stage_total === "number" ? p.stage_total : null,
          });
        } else if (p.state === "error") {
          setRetryStatus({
            state: "error",
            message: p.error || p.message || "エラー",
            progress: 0,
          });
        }
      } catch {/* ignore */}
    })();
    return () => { cancelled = true; };
  }, [isLive, sessionId]);

  /* 要約ジョブ状態 (in-flight / done / failed / cancelled) */
  const [summaryJob, setSummaryJob] = useState<SummarizeStatus | null>(null);
  const [summaryBusy, setSummaryBusy] = useState(false);   // 連打debounce
  // mount 時 / minutes 確定時に in-flight ジョブが無いか確認。
  // pipeline-detail (live) モードでも minutesId が手に入った瞬間に呼ぶことで、
  // 文字起こし完了直後に走る自動要約のステータスを取りこぼさない。
  useEffect(() => {
    if (!minutesId) return;
    let cancelled = false;
    (async () => {
      try {
        const st = await getSummarizeStatus(minutesId);
        if (cancelled) return;
        if (st.state && st.state !== "none") {
          setSummaryJob(st);
        }
      } catch {/* ignore */}
    })();
    return () => { cancelled = true; };
  }, [minutesId]);

  const startSummarize = useCallback(async (provider?: SummarizeProvider) => {
    if (!minutesId || summaryBusy) return;
    setSummaryBusy(true);
    try {
      // 楽観的に running 状態へ。WS で確定値が来たら上書きされる。
      setSummaryJob({
        state: "queued",
        minutes_id: minutesId,
        provider: provider ?? null,
        partial_text: "",
      });
      await triggerSummarize(minutesId, provider ?? null);
    } catch (e) {
      setSummaryJob({
        state: "failed",
        minutes_id: minutesId,
        provider: provider ?? null,
        error_code: "REQUEST_FAILED",
        error_message: e instanceof Error ? e.message : "起動失敗",
      });
    } finally {
      setSummaryBusy(false);
    }
  }, [minutesId, summaryBusy]);

  const handleCancelSummary = useCallback(async () => {
    if (!minutesId) return;
    try { await cancelSummarize(minutesId); } catch {/* ignore */}
  }, [minutesId]);

  /* minutesId が手に入った時点で WS を貼り、pipeline_done での reload と
     要約イベントを購読する。pipeline-detail (live) モードでも文字起こし完了で
     minutesId が確定し次第購読を開始するので、自動要約のイベントを取りこぼさない。 */
  useEffect(() => {
    if (!minutesId || !sessionId) return;
    let ws: WebSocket | null = null;
    let timer: number | null = null;
    let attempt = 0;
    let stopped = false;

    const reload = async () => {
      try {
        const fresh = await getMinutes(minutesId);
        setCurrentMinutes(fresh);
        // 一覧画面など他コンポーネントも同期できるよう通知
        window.dispatchEvent(
          new CustomEvent("minutes-updated", { detail: { id: minutesId } }),
        );
      } catch {}
    };
    const handle = (e: MessageEvent) => {
      try {
        const m = JSON.parse(e.data);
        // retry banner は「既存議事録の再文字起こし」を可視化するためのもの。
        // pipeline-detail (live) モードでは初回文字起こしの pipeline_progress と
        // 区別できないので、live モードでは setRetryStatus を呼ばない。
        if (!isLive && m.type === "pipeline_progress" && m.data?.session_id === sessionId) {
          const st = String(m.data?.state || "");
          if (["transcribing", "stopping"].includes(st)) {
            setRetryStatus({
              state: st as "transcribing" | "stopping",
              message: String(m.data?.message || "処理中..."),
              progress: typeof m.data?.progress === "number" ? m.data.progress : 0,
              stage: m.data?.stage ?? null,
              stage_label: m.data?.stage_label ?? null,
              stage_step: typeof m.data?.stage_step === "number" ? m.data.stage_step : null,
              stage_total: typeof m.data?.stage_total === "number" ? m.data.stage_total : null,
            });
          }
        }
        if (m.type === "pipeline_done" && m.data?.session_id === sessionId) {
          if (!isLive) setRetryStatus(null);
          reload();
        }
        if (!isLive && m.type === "pipeline_error" && m.data?.session_id === sessionId) {
          setRetryStatus({
            state: "error",
            message: String(m.data?.message || "エラー"),
            progress: 0,
          });
        }
        if (m.type === "speaker_renamed" && m.data?.speaker_id && m.data?.speaker_label) {
          const speakerId = String(m.data.speaker_id);
          const speakerLabel = String(m.data.speaker_label);
          setCurrentMinutes((prev) => {
            if (!prev) return prev;
            const transcript = (prev.transcript || []).map((seg) =>
              seg.speaker_id === speakerId
                ? { ...seg, speaker_label: speakerLabel }
                : seg
            );
            return { ...prev, transcript };
          });
        }

        // ─── 要約ジョブイベント ───
        if (m.data?.minutes_id !== minutesId) {
          // 自分の議事録に対するイベントだけ拾う
        } else if (m.type === "summary_stage") {
          setSummaryJob((prev) => ({
            ...(prev || { minutes_id: minutesId, state: "running" as const }),
            state: prev?.state === "done" ? "done" : "running",
            stage: m.data?.stage ?? null,
            stage_label: m.data?.stage_label ?? null,
          }));
        } else if (m.type === "summary_activity") {
          const activity = String(m.data?.activity || "").trim();
          if (activity) {
            setSummaryJob((prev) => ({
              ...(prev || { minutes_id: minutesId, state: "running" as const }),
              state: prev?.state === "done" ? "done" : "running",
              activity,
            }));
          }
        } else if (m.type === "summary_chunk") {
          const chunk = String(m.data.text || "");
          setSummaryJob((prev) => ({
            ...(prev || { minutes_id: minutesId, state: "running" }),
            state: "running",
            partial_text: (prev?.partial_text || "") + chunk,
            error_code: null,
            error_message: null,
          }));
        } else if (m.type === "summary_done") {
          setSummaryJob({
            state: "done",
            minutes_id: minutesId,
            provider: m.data?.provider ?? null,
            model: m.data?.model ?? null,
            finished_at: Date.now() / 1000,
          });
          // タイトル即時反映 (DB は既に更新済み、UI 楽観反映)
          if (m.data?.new_title) {
            setCurrentMinutes((prev) =>
              prev ? { ...prev, title: String(m.data.new_title) } : prev,
            );
          }
          reload();
        } else if (m.type === "minutes_title_updated") {
          // 別箇所からの更新 (一覧画面等) を反映
          setCurrentMinutes((prev) =>
            prev ? { ...prev, title: String(m.data?.title || prev.title) } : prev,
          );
        } else if (m.type === "summary_failed") {
          setSummaryJob({
            state: "failed",
            minutes_id: minutesId,
            error_code: m.data?.error_code ?? "UNKNOWN",
            error_message: m.data?.message ?? "要約生成に失敗しました",
          });
        } else if (m.type === "summary_cancelled") {
          setSummaryJob((prev) => ({
            ...(prev || { minutes_id: minutesId }),
            state: "cancelled",
          }));
        } else if (m.type === "summary_skipped") {
          setSummaryJob({
            state: "skipped",
            minutes_id: minutesId,
            error_code: m.data?.reason ?? "SKIPPED",
            error_message: "発話が短すぎて要約をスキップしました",
          });
        }
      } catch {}
    };
    const connect = () => {
      if (stopped) return;
      try { ws = new WebSocket(WS_URL); } catch { scheduleReconnect(); return; }
      ws.onopen = () => { attempt = 0; };
      ws.onmessage = handle;
      ws.onclose = scheduleReconnect;
      ws.onerror = () => { try { ws?.close(); } catch {} };
    };
    const scheduleReconnect = () => {
      if (stopped || timer) return;
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
  }, [isLive, minutesId, sessionId]);

  /* audio player */
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState(false);
  const [ct, setCt] = useState(0);
  const [rate, setRate] = useState(1.0);
  const [mediaDuration, setMediaDuration] = useState<number | null>(null);
  const [audioInfo, setAudioInfo] = useState<{ name: string; size_bytes: number } | null>(null);
  const playbackDuration = (mediaDuration && Number.isFinite(mediaDuration) && mediaDuration > 0)
    ? mediaDuration
    : duration;

  useEffect(() => {
    if (!sessionId) {
      setAudioInfo(null);
      setMediaDuration(null);
      return;
    }
    let cancelled = false;
    getSessionAudioInfo(sessionId)
      .then((info) => {
        if (!cancelled) setAudioInfo(info);
      })
      .catch(() => {
        if (!cancelled) setAudioInfo(null);
      });
    return () => { cancelled = true; };
  }, [sessionId]);

  // sessionId 変更時 / unmount 時に再生中の Audio を必ず停止する
  useEffect(() => {
    return () => {
      const a = audioRef.current;
      if (a) {
        try { a.pause(); } catch { /* noop */ }
        a.onended = null;
        a.ontimeupdate = null;
        try { a.src = ""; a.load(); } catch { /* noop */ }
        audioRef.current = null;
      }
      setPlaying(false);
      setCt(0);
      setMediaDuration(null);
    };
  }, [sessionId]);

  // playbackRate の同期 (audio が未生成なら次回 aud() で適用)
  useEffect(() => {
    const a = audioRef.current;
    if (a) {
      try { a.playbackRate = rate; } catch { /* noop */ }
    }
  }, [rate]);
  const initialQuery = !isLive ? (props as MinutesProps).initialQuery : undefined;
  const initialTab = !isLive ? (props as MinutesProps).initialTab : undefined;
  const [tab, setTab] = useState<"summary" | "transcript">(
    initialTab ?? (summary ? "summary" : "transcript")
  );

  /* in-document find */
  const [findOpen, setFindOpen] = useState<boolean>(!!initialQuery);
  const [findQuery, setFindQuery] = useState<string>(initialQuery || "");
  const [findIndex, setFindIndex] = useState(0);
  const findInputRef = useRef<HTMLInputElement | null>(null);
  const FIND_ACTIVE_ID = "detail-find-active";

  /* transcript scroll container (for minimap) */
  const transcriptScrollRef = useRef<HTMLDivElement | null>(null);

  // 検索ヒット位置はタブに関係なく常に計算しておく。
  // タブバッジでヒット件数を出すため、また Summary タブを開いていても
  // Transcribe 側の件数を即座に把握できるようにするため。
  const matches = useMemo(() => {
    const q = findQuery.trim();
    if (!q) return [] as Array<{ segIndex: number; start: number; end: number }>;
    const out: Array<{ segIndex: number; start: number; end: number }> = [];
    const lcq = q.toLowerCase();
    transcript.forEach((seg, i) => {
      const text = seg.text || "";
      const lct = text.toLowerCase();
      let pos = 0;
      while (pos < lct.length) {
        const found = lct.indexOf(lcq, pos);
        if (found < 0) break;
        out.push({ segIndex: i, start: found, end: found + q.length });
        pos = found + q.length;
      }
    });
    return out;
  }, [transcript, findQuery]);

  // 要約タブの検索ヒット件数は MarkdownView (tiptap) 側で実体テキストから数える。
  // (markdown 構文文字を含めない正確な件数 + 各マッチに decoration を貼るため)
  const [summaryMatchCount, setSummaryMatchCount] = useState(0);

  // タブ切替時に findIndex を 0 に戻す (各タブごとにマッチ集合が違うため)
  useEffect(() => { setFindIndex(0); }, [tab]);

  // アクティブタブのマッチ件数
  const activeMatchCount = tab === "summary" ? summaryMatchCount : matches.length;

  // findIndex がレンジ外にならないようにクランプ
  useEffect(() => {
    if (activeMatchCount === 0) {
      if (findIndex !== 0) setFindIndex(0);
    } else if (findIndex >= activeMatchCount) {
      setFindIndex(0);
    }
  }, [activeMatchCount, findIndex]);

  // 検索クエリ/インデックスが変わったら active な mark を画面中央へ (文字起こしタブ用)
  useEffect(() => {
    if (tab !== "transcript" || matches.length === 0) return;
    const id = requestAnimationFrame(() => {
      const el = document.getElementById(FIND_ACTIVE_ID);
      if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
    });
    return () => cancelAnimationFrame(id);
  }, [findIndex, matches.length, findQuery, tab]);

  const nextMatch = () => {
    if (activeMatchCount === 0) return;
    setFindIndex((i) => (i + 1) % activeMatchCount);
  };
  const prevMatch = () => {
    if (activeMatchCount === 0) return;
    setFindIndex((i) => (i - 1 + activeMatchCount) % activeMatchCount);
  };

  const openFind = () => {
    setFindOpen(true);
    requestAnimationFrame(() => {
      findInputRef.current?.focus();
      findInputRef.current?.select();
    });
  };
  const closeFind = () => {
    setFindOpen(false);
  };

  // Cmd/Ctrl+F でいつでも開く
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "f") {
        e.preventDefault();
        openFind();
      } else if (e.key === "Escape" && findOpen) {
        // find バーが開いていて Esc なら閉じる
        const target = e.target as HTMLElement | null;
        if (target?.tagName === "INPUT" && target === findInputRef.current) {
          e.preventDefault();
          closeFind();
        } else if (target?.tagName !== "INPUT" && target?.tagName !== "TEXTAREA") {
          closeFind();
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [findOpen]);

  // 初期クエリ付きで開かれた場合、レイアウト確定後に確実にスクロールさせるためのリトライ
  useEffect(() => {
    if (!initialQuery) return;
    let cancelled = false;
    let tries = 0;
    const tick = () => {
      if (cancelled) return;
      const el = document.getElementById(FIND_ACTIVE_ID);
      if (el) {
        el.scrollIntoView({ block: "center", behavior: "auto" });
        return;
      }
      tries++;
      if (tries < 10) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const [busy, setBusy] = useState<"export" | "copy" | "delete" | "retry" | "retry_cancel" | "recover" | null>(null);

  /* title edit */
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [titleSaving, setTitleSaving] = useState(false);
  const titleInputRef = useRef<HTMLInputElement | null>(null);
  const titleIme = useImeSafeEnter();

  useEffect(() => {
    if (editingTitle && titleInputRef.current) {
      titleInputRef.current.focus();
      titleInputRef.current.select();
    }
  }, [editingTitle]);

  const canEditTitle = !isLive && !!minutesId;

  const startEditTitle = () => {
    if (!canEditTitle) return;
    setTitleDraft(title);
    setEditingTitle(true);
  };

  const cancelEditTitle = () => {
    setEditingTitle(false);
    setTitleDraft("");
  };

  const commitEditTitle = async () => {
    if (!minutesId) { cancelEditTitle(); return; }
    const next = titleDraft.trim();
    if (!next || next === title) { cancelEditTitle(); return; }
    setTitleSaving(true);
    try {
      const updated = await updateMinutesTitle(minutesId, next);
      setCurrentMinutes(updated);
      setEditingTitle(false);
      setTitleDraft("");
      window.dispatchEvent(new CustomEvent("minutes-updated", { detail: { id: minutesId } }));
    } catch (e) {
      showToast({ kind: "err", text: `タイトル更新失敗: ${e instanceof Error ? e.message : ""}` });
    } finally {
      setTitleSaving(false);
    }
  };

  const aud = () => {
    if (!audioRef.current) {
      const a = new Audio(audioPlayUrl(sessionId));
      const syncDuration = () => {
        const d = a.duration;
        if (Number.isFinite(d) && d > 0) setMediaDuration(d);
      };
      a.onloadedmetadata = syncDuration;
      a.ondurationchange = syncDuration;
      a.onended = () => {
        const d = a.duration;
        const end = Number.isFinite(d) && d > 0 ? d : (a.currentTime || 0);
        setCt(end);
        setPlaying(false);
      };
      a.ontimeupdate = () => setCt(a.currentTime || 0);
      try { a.playbackRate = rate; } catch { /* noop */ }
      audioRef.current = a;
    }
    return audioRef.current;
  };
  const toggle = () => {
    const a = aud();
    if (playing) { a.pause(); setPlaying(false); }
    else { a.play(); setPlaying(true); }
  };
  const seek = (t: number) => {
    const a = aud(); a.currentTime = t; a.play(); setPlaying(true);
  };

  const handleCopy = async () => {
    if (!minutesId || busy) return;
    setBusy("copy");
    try {
      const r = await getMinutesMarkdown(minutesId);
      // バックエンド (pbcopy) 経由で確実にコピー (Tauri webview の制約を回避)
      await writeClipboard(r.content);
      showToast({ kind: "ok", text: "コピーしました" });
    } catch (e) {
      showToast({ kind: "err", text: `コピー失敗: ${e instanceof Error ? e.message : "不明なエラー"}` });
    } finally { setBusy(null); }
  };
  const handleExport = async () => {
    if (!minutesId || busy) return;
    setBusy("export");
    try {
      const r = await exportMinutes(minutesId);
      const filename = r.path.split("/").pop() || r.path;
      showToast({
        kind: "ok",
        text: `保存: ${filename} ・クリックで開く`,
        hoverHint: r.path,
        onClick: async () => {
          try {
            await openInDefaultApp(r.path);
          } catch (err) {
            const msg = err instanceof Error
              ? err.message
              : typeof err === "string"
                ? err
                : JSON.stringify(err);
            showToast({
              kind: "err",
              text: `開けませんでした: ${msg}`,
            });
          }
        },
      });
    } catch (e) {
      showToast({ kind: "err", text: `書き出し失敗: ${e instanceof Error ? e.message : "不明なエラー"}` });
    } finally { setBusy(null); }
  };
  const handleRetry = async () => {
    if (!minutesId || busy || isRetranscribing) return;
    const ok = await ask(
      "文字起こしを再実行します。録音長によっては数分かかります。続行しますか?",
      {
        title: "文字起こしの再実行",
        kind: "info",
        okLabel: "実行",
        cancelLabel: "キャンセル",
      },
    );
    if (!ok) return;
    setBusy("retry");
    setRetryStatus({ state: "queued", message: "再実行をキューに追加中...", progress: 0 });
    try {
      await retranscribeMinutes(minutesId);
      showToast({ kind: "ok", text: "再実行を開始しました" });
    } catch (e) {
      showToast({ kind: "err", text: `再実行失敗: ${e instanceof Error ? e.message : ""}` });
      setRetryStatus(null);
    } finally {
      setBusy(null);
    }
  };

  const handleCancelRetry = async () => {
    if (!minutesId || busy === "retry_cancel" || !isRetranscribing) return;
    setBusy("retry_cancel");
    try {
      const r = await cancelRetranscribeMinutes(minutesId);
      if (r.status === "not_running") {
        setRetryStatus(null);
      } else {
        setRetryStatus((prev) => (
          prev
            ? { ...prev, state: "stopping", message: "停止中...", progress: prev.progress ?? 0 }
            : { state: "stopping", message: "停止中...", progress: 0 }
        ));
      }
    } catch (e) {
      showToast({ kind: "err", text: `停止失敗: ${e instanceof Error ? e.message : ""}` });
    } finally {
      setBusy(null);
    }
  };

  const handleDelete = async () => {
    if (!minutesId || busy) return;
    const ok = await ask("この議事録を削除しますか?", {
      title: "削除の確認",
      kind: "warning",
      okLabel: "削除",
      cancelLabel: "キャンセル",
    });
    if (!ok) return;
    setBusy("delete");
    try {
      await deleteMinutes(minutesId);
      props.onDeleted?.();
      props.onBack();
    } catch (e) {
      showToast({ kind: "err", text: `削除失敗: ${e instanceof Error ? e.message : "不明なエラー"}` });
      setBusy(null);
    }
  };

  const handleRecoverSession = async (startRetranscribe: boolean) => {
    if (!sessionId || busy) return;
    setBusy("recover");
    try {
      const res = await recoverSessionMinutes(sessionId, startRetranscribe);
      setCurrentMinutes(res.minutes);
      window.dispatchEvent(new CustomEvent("minutes-updated"));
      if (startRetranscribe) {
        const retryState = String(res.retranscribe?.status || "");
        if (retryState === "started") {
          showToast({ kind: "ok", text: "救済保存して再文字起こしを開始しました" });
        } else if (retryState === "skipped") {
          showToast({ kind: "info", text: "救済保存しました（音声がないため再文字起こしはスキップ）" });
        } else {
          showToast({ kind: "ok", text: "救済保存しました" });
        }
      } else {
        showToast({ kind: "ok", text: "救済保存しました" });
      }
    } catch (e) {
      showToast({ kind: "err", text: `救済保存に失敗: ${e instanceof Error ? e.message : "不明なエラー"}` });
    } finally {
      setBusy(null);
    }
  };

  const canAct = !!minutesId;
  const canRecoverSession = isLive && !minutesId && !!sessionId
    && (stateLive === "error" || stateLive === "done");
  const isRetranscribing = !!retryStatus && retryStatus.state !== "error";
  const retryStopPending = busy === "retry_cancel" || retryStatus?.state === "stopping";
  const isSearching = findQuery.trim().length > 0;
  const tabs: Array<["summary" | "transcript", string, number | null]> = [
    [
      "summary",
      "Summary",
      isSearching ? summaryMatchCount : (summary ? null : 0),
    ],
    [
      "transcript",
      "Transcribe",
      isSearching ? matches.length : transcript.length,
    ],
  ];

  const progress = playbackDuration > 0 ? (ct / playbackDuration) * 100 : 0;

  return (
    <div className="anim-fade-in flex flex-col h-full overflow-hidden">
      {/* Header (toolbar 風) */}
      <div className="toolbar">
        <div className="flex items-center gap-2 flex-1 min-w-0">
          <button onClick={props.onBack} className="icon-btn" title="戻る">
            <ArrowLeft size={14} weight="regular" />
          </button>
          <div className="min-w-0 flex-1">
            {editingTitle ? (
              <input
                ref={titleInputRef}
                type="text"
                value={titleDraft}
                onChange={(e) => setTitleDraft(e.target.value)}
                {...titleIme.imeHandlers}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    if (titleIme.isImeEnter(e)) return;
                    e.preventDefault();
                    commitEditTitle();
                  } else if (e.key === "Escape") {
                    e.preventDefault();
                    cancelEditTitle();
                  }
                }}
                onBlur={commitEditTitle}
                disabled={titleSaving}
                className="title-edit-input"
                placeholder="タイトル"
                maxLength={200}
              />
            ) : (
              <div
                className={`title-row ${canEditTitle ? "is-editable" : ""}`}
                onDoubleClick={() => canEditTitle && startEditTitle()}
                onContextMenu={(e) => {
                  if (!canEditTitle) return;
                  e.preventDefault();
                  startEditTitle();
                }}
                title={canEditTitle ? "ダブルクリック / 右クリックで名前を編集" : undefined}
              >
                <h2 className="text-[14px] font-semibold text-(--t1) truncate">{title}</h2>
                {canEditTitle && (
                  <button
                    type="button"
                    onClick={startEditTitle}
                    className="title-edit-btn"
                    aria-label="名前を編集"
                    title="名前を編集"
                  >
                    <PencilSimple size={11} weight="regular" />
                  </button>
                )}
              </div>
            )}
            {(date || duration > 0) && (
              <p className="text-[11px] text-(--t3) mt-0.5">
                {date}{date && duration > 0 && " · "}{duration > 0 && fmtDuration(duration)}
              </p>
            )}
          </div>
        </div>

        {canAct && (
          <div className="flex items-center gap-1 shrink-0">
            {isRetranscribing ? (
              <ActionBtn
                onClick={handleCancelRetry}
                loading={retryStopPending}
                disabled={retryStopPending}
                title="進行中の再文字起こしを停止"
                label={retryStopPending ? "停止中" : "停止"}
                danger
              >
                <Stop size={13} weight="regular" />
              </ActionBtn>
            ) : (
              <ActionBtn
                onClick={handleRetry}
                loading={busy === "retry"}
                title="文字起こしを再実行"
                label="再実行"
              >
                <ArrowsClockwise size={13} weight="regular" />
              </ActionBtn>
            )}
            <ActionBtn onClick={handleCopy} loading={busy === "copy"} title="Markdown をコピー" label="コピー">
              <Copy size={13} weight="regular" />
            </ActionBtn>
            <ActionBtn onClick={handleExport} loading={busy === "export"} title="ファイルに書き出し" label="書き出し">
              <DownloadSimple size={13} weight="regular" />
            </ActionBtn>
            <ActionBtn onClick={handleDelete} loading={busy === "delete"} title="削除" danger>
              <Trash size={13} weight="regular" />
            </ActionBtn>
          </div>
        )}
      </div>

      {/* Live processing state — 録音停止後の pipeline (stopping / transcribing) も
          再文字起こしと同じ retry-status バナーで表示する */}
      {isLive && (stateLive === "stopping" || stateLive === "transcribing") && (
        <div className="retry-status">
          <div className="retry-status-row">
            <Spinner size={12} />
            <div className="flex-1 min-w-0 flex items-center gap-2">
              {pipeline?.stage_step && pipeline?.stage_total && (
                <span className="num text-[10px] text-(--t3) tabular-nums shrink-0 px-1.5 py-0.5 rounded bg-(--surface-2)">
                  {pipeline.stage_step}/{pipeline.stage_total}
                </span>
              )}
              <span className="text-[12px] text-(--t2) truncate">
                {pipeline?.stage_label
                  ? <><span className="font-medium text-(--t1)">{pipeline.stage_label}</span>{liveMessage && liveMessage !== pipeline.stage_label ? <span className="text-(--t3)"> · {liveMessage}</span> : null}</>
                  : (liveMessage || "処理中...")}
              </span>
            </div>
            {typeof pipeline?.progress === "number" && pipeline.progress > 0 && (
              <span className="num text-[11px] text-(--t3) tabular-nums shrink-0">
                {Math.round(pipeline.progress * 100)}%
              </span>
            )}
          </div>
          <div className="retry-progress">
            <div
              className="retry-progress-fill"
              style={{
                width: `${Math.max(
                  2,
                  Math.round(
                    ((pipeline?.progress && pipeline.progress > 0)
                      ? pipeline.progress
                      : (pipeline?.stage_step && pipeline?.stage_total
                          ? (pipeline.stage_step - 1) / pipeline.stage_total
                          : 0)
                    ) * 100,
                  ),
                )}%`,
              }}
            />
          </div>
        </div>
      )}
      {(liveError || canRecoverSession) && (
        <div className="px-4 py-2 border-b border-(--border) flex items-center gap-2">
          {liveError && (
            <p className="text-[11px] text-(--danger) flex-1 min-w-0 truncate">{liveError}</p>
          )}
          {canRecoverSession && (
            <div className="flex items-center gap-1 shrink-0">
              <ActionBtn
                onClick={() => handleRecoverSession(false)}
                loading={busy === "recover"}
                disabled={busy === "recover"}
                title="既存の文字起こしを議事録として保存"
                label="救済保存"
              >
                <DownloadSimple size={13} weight="regular" />
              </ActionBtn>
              <ActionBtn
                onClick={() => handleRecoverSession(true)}
                loading={busy === "recover"}
                disabled={busy === "recover"}
                title="救済保存して再文字起こしを開始"
                label="救済して再実行"
              >
                <ArrowsClockwise size={13} weight="regular" />
              </ActionBtn>
            </div>
          )}
        </div>
      )}

      {/* Retranscribe status banner (非-live のみ) */}
      {!isLive && retryStatus && (
        <div className="retry-status">
          <div className="retry-status-row">
            {retryStatus.state === "error" ? (
              <span className="text-(--danger)" aria-label="error">⚠</span>
            ) : (
              <Spinner size={12} />
            )}
            <div className="flex-1 min-w-0 flex items-center gap-2">
              {retryStatus.stage_step && retryStatus.stage_total && retryStatus.state !== "error" && (
                <span className="num text-[10px] text-(--t3) tabular-nums shrink-0 px-1.5 py-0.5 rounded bg-(--surface-2)">
                  {retryStatus.stage_step}/{retryStatus.stage_total}
                </span>
              )}
              <span className={`text-[12px] truncate ${retryStatus.state === "error" ? "text-(--danger)" : "text-(--t2)"}`}>
                {retryStatus.stage_label
                  ? <><span className="font-medium text-(--t1)">{retryStatus.stage_label}</span>{retryStatus.message && retryStatus.message !== retryStatus.stage_label ? <span className="text-(--t3)"> · {retryStatus.message}</span> : null}</>
                  : retryStatus.message}
              </span>
            </div>
            {retryStatus.state !== "error" && (
              <span className="num text-[11px] text-(--t3) tabular-nums shrink-0">
                {Math.round((retryStatus.progress || 0) * 100)}%
              </span>
            )}
            {retryStatus.state === "error" && (
              <button
                onClick={() => setRetryStatus(null)}
                className="icon-btn !w-6 !h-6"
                title="閉じる"
              >
                <X size={11} weight="bold" />
              </button>
            )}
          </div>
          {retryStatus.state !== "error" && (
            <div className="retry-progress">
              <div
                className="retry-progress-fill"
                style={{
                  width: `${Math.max(
                    2,
                    Math.round(
                      // sub-progress (チャンク) があればそれ、無ければ stage_step/total を使う
                      ((retryStatus.progress && retryStatus.progress > 0)
                        ? retryStatus.progress
                        : (retryStatus.stage_step && retryStatus.stage_total
                            ? (retryStatus.stage_step - 1) / retryStatus.stage_total
                            : 0)
                      ) * 100,
                    ),
                  )}%`,
                }}
              />
            </div>
          )}
        </div>
      )}

      {/* Player */}
      {sessionId && playbackDuration > 0 && (
        <div className="relative border-b border-(--border) bg-(--surface)">
          <div className="px-4 pt-3 pb-1">
            <Waveform level={playing ? 0.5 : 0.1} alive={playing} height={64} />
          </div>
          {/* progress overlay */}
          <div className="absolute top-0 bottom-0 pointer-events-none"
            style={{
              left: 0,
              width: `${progress}%`,
              background: "color-mix(in srgb, var(--accent) 12%, transparent)",
              borderRight: progress > 0 ? "1px solid var(--accent)" : "none",
              transition: "width 100ms linear",
            }} />
          {/* seek hit area */}
          <div className="absolute inset-0 cursor-pointer"
            onClick={(e) => {
              const r = e.currentTarget.getBoundingClientRect();
              seek(((e.clientX - r.left) / r.width) * playbackDuration);
            }} />

          <div className="relative flex items-center justify-between px-4 pb-2 gap-3">
            <div className="num text-[11px] text-(--t3) shrink-0">{fmt(ct)}</div>
            {/* Play button: bottom row の幾何学的中心に重ねる */}
            <button
              onClick={(e) => { e.stopPropagation(); toggle(); }}
              className="absolute left-1/2 -bottom-2 -translate-x-1/2 -translate-y-1/2 z-10 circle-btn !w-8 !h-8 hover:opacity-80 duration-100 transition-opacity"
              title={playing ? "一時停止" : "再生"}
            >
              {playing ? <Pause size={12} weight="fill" /> : <Play size={12} weight="fill" />}
            </button>
            <div className="flex items-center gap-2 shrink-0">
              <div className="num text-[11px] text-(--t3)">{fmt(playbackDuration)}</div>
              {audioInfo && (
                <span
                  className="num text-[11px] text-(--t3) tabular-nums"
                  title={audioInfo.name}
                >
                  {fmtBytes(audioInfo.size_bytes)}
                </span>
              )}
              <PlaybackRatePill rate={rate} onChange={setRate} />
            </div>
          </div>
        </div>
      )}

      {/* Tabs */}
      <div className="flex items-center gap-5 px-5 border-b border-(--border)">
        {tabs.map(([k, label, count]) => {
          const active = tab === k;
          return (
            <button key={k} onClick={() => setTab(k)}
              className="relative py-2.5 cursor-pointer flex items-center gap-1.5"
              style={{ background: "transparent", border: "none" }}>
              <span className={`text-[12px] font-medium transition-colors ${active ? "text-(--t1)" : "text-(--t3) hover:text-(--t2)"}`}>
                {label}
              </span>
              {count !== null && count !== undefined && (
                <span className="text-[10px] text-(--t3) tabular-nums">{count}</span>
              )}
              {active && (
                <span className="absolute -bottom-px left-0 right-0 h-[2px] rounded-full"
                  style={{ background: "var(--accent)" }} />
              )}
            </button>
          );
        })}
        <div className="ml-auto">
          {!findOpen && (
            <button
              onClick={openFind}
              className="icon-btn"
              title="本文を検索 (⌘F)"
              aria-label="本文を検索"
            >
              <MagnifyingGlass size={13} weight="regular" />
            </button>
          )}
        </div>
      </div>

      {/* Find bar */}
      {findOpen && (
        <FindBar
          query={findQuery}
          onChange={(v) => { setFindQuery(v); setFindIndex(0); }}
          inputRef={findInputRef}
          matches={activeMatchCount}
          index={activeMatchCount === 0 ? 0 : findIndex + 1}
          otherCount={tab === "summary" ? matches.length : summaryMatchCount}
          activeTab={tab}
          onJumpSummary={() => { setTab("summary"); }}
          onJumpTranscript={() => { setTab("transcript"); }}
          onPrev={prevMatch}
          onNext={nextMatch}
          onClose={closeFind}
        />
      )}

      {/* Content */}
      <div className="flex-1 flex overflow-hidden min-h-0">
        <div ref={transcriptScrollRef} className="flex-1 overflow-y-auto">
        {tab === "summary" && (
          <SummaryPanel
            minutesId={minutesId}
            savedSummary={summary || ""}
            savedModel={minutes?.llm_model || ""}
            job={summaryJob}
            canAct={canAct}
            busy={summaryBusy}
            searchQuery={findOpen ? findQuery : ""}
            activeMatchIndex={tab === "summary" ? findIndex : -1}
            onMatchesChange={setSummaryMatchCount}
            onGenerate={startSummarize}
            onCancel={handleCancelSummary}
            onSaved={(next) => {
              setCurrentMinutes(next);
              window.dispatchEvent(
                new CustomEvent("minutes-updated", { detail: { id: next.id } })
              );
            }}
          />
        )}

        {tab === "transcript" && (
          transcript.length > 0 ? (
            <div className="anim-fade-in">
              {transcript.map((seg, i) => {
                const on = playing && ct >= seg.start && ct < seg.end;
                const speakerLabel = seg.speaker_label || "話者?";
                const segMatches = matches.filter((m) => m.segIndex === i);
                return (
                  <div
                    key={i}
                    onClick={() => seek(seg.start)}
                    className="flat-row"
                  >
                    <div className="min-w-[74px] shrink-0 flex flex-col">
                      <span className="num text-[10px] text-(--t3)" style={{ fontWeight: 400 }}>
                        {fmt(seg.start)}
                      </span>
                      <span className="text-[10px] text-(--t3)">{speakerLabel}</span>
                    </div>
                    <span className={`text-[13px] leading-relaxed flex-1 transition-colors ${on ? "text-(--t1)" : "text-(--t2)"}`}>
                      <SegmentText
                        text={seg.text}
                        matches={segMatches}
                        allMatches={matches}
                        activeIndex={findIndex}
                        activeId={FIND_ACTIVE_ID}
                      />
                    </span>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="flex items-center justify-center h-full">
              <p className="text-[12px] text-(--t3)">
                {isLive ? "発話を待機中..." : "音声が検出されませんでした"}
              </p>
            </div>
          )
        )}
        </div>
        {tab === "transcript" && transcript.length > 0 && (
          <TranscriptMinimap
            segments={transcript}
            scrollRef={transcriptScrollRef}
            matches={matches}
            findIndex={findIndex}
            onSegmentClick={(i) => {
              const el = transcriptScrollRef.current?.querySelectorAll(".flat-row")[i] as HTMLElement | undefined;
              el?.scrollIntoView({ block: "center", behavior: "smooth" });
            }}
          />
        )}
      </div>
    </div>
  );
}

function ActionBtn({
  children, onClick, loading, title, label, danger, disabled,
}: {
  children: React.ReactNode;
  onClick: () => void;
  loading?: boolean;
  disabled?: boolean;
  title: string;
  label?: string;
  danger?: boolean;
}) {
  const blocked = Boolean(loading || disabled);
  return (
    <button onClick={onClick} disabled={blocked} title={title}
      className={`btn h-8 px-3 text-[11px] ${danger ? "btn-danger btn-ghost" : "btn-ghost"}`}>
      {loading ? <Spinner size={12} /> : children}
      {label && <span>{label}</span>}
    </button>
  );
}

/* ───────── Playback Rate Pill ─────────
   再生 UI 右下に置く 0.5x〜2x プリセット選択ピル。1.0x のときは色をプレーンに保ち、
   それ以外で accent カラーで強調する (修飾済みであることが一目で分かるように)。 */
const RATE_OPTIONS = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0];

function PlaybackRatePill({
  rate, onChange,
}: { rate: number; onChange: (r: number) => void }) {
  // value/option は文字列キーが必要なので 0.5 → "0.50" の固定桁で持つ。
  const toKey = (r: number) => r.toFixed(2);
  const fmtLabel = (r: number) => `${r.toFixed(2).replace(/0$/, "")}x`;
  const modified = Math.abs(rate - 1.0) > 1e-3;

  return (
    <Select
      value={toKey(rate)}
      onChange={(v) => onChange(parseFloat(v))}
      options={RATE_OPTIONS.map((r) => ({
        value: toKey(r),
        label: fmtLabel(r),
      }))}
      size="sm"
      title="再生速度"
      ariaLabel="再生速度"
      className={`rate-select ${modified ? "is-modified" : ""}`}
    />
  );
}

/* ───────── Summary Panel ─────────
   要約タブのコンテンツ全体を担当。状態を統合管理:
     - in-flight (queued/running): スピナー + ストリーミング text (read-only)
     - failed: エラーバナー + リトライ
     - skipped: 案内のみ
     - done & summary 有り: Markdown 表示 + footer (provider/model + 再要約 + 編集)
     - 編集モード: TipTap WYSIWYG エディタ + 保存/破棄
     - summary 無し & job なし: 空状態 + 「要約を生成」ボタン
   ボタン高さは ActionBtn と同じ h-8 で揃え、視覚的に統一感を保つ。 */
function SummaryPanel({
  minutesId, savedSummary, savedModel, job, canAct, busy,
  searchQuery, activeMatchIndex, onMatchesChange,
  onGenerate, onCancel, onSaved,
}: {
  minutesId?: string;
  savedSummary: string;
  savedModel: string;
  job: SummarizeStatus | null;
  canAct: boolean;
  busy: boolean;
  searchQuery?: string;
  activeMatchIndex?: number;
  onMatchesChange?: (n: number) => void;
  onGenerate: (provider?: SummarizeProvider) => void;
  onCancel: () => void;
  onSaved?: (next: Minutes) => void;
}) {
  const state = job?.state;
  const isRunning = state === "queued" || state === "running";
  const isFailed = state === "failed";
  const isSkipped = state === "skipped";
  const isCancelled = state === "cancelled";

  // 表示するMarkdown本文の決定:
  // 生成中は partial_text を表示 (ストリーミング)
  // 完了後は savedSummary (DB 由来)。ただし summary_done 直後で reload が間に合っていない
  // 場合 (savedSummary が空) は partial_text を fallback で表示し続けてフリッカーを防ぐ。
  const displayText = isRunning
    ? (job?.partial_text || savedSummary || "")
    : (savedSummary || job?.partial_text || "");

  // Status banner があるかどうか (生成中・失敗・skip・cancel)
  const showBanner = isRunning || isFailed || isSkipped || isCancelled;

  // 編集モード state
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(savedSummary);
  const [saving, setSaving] = useState(false);

  // savedSummary が外から変わったら draft も同期 (生成完了時など)
  useEffect(() => {
    if (!editing) setDraft(savedSummary);
  }, [savedSummary, editing]);

  const canEdit = !isRunning && !!minutesId;
  const dirty = draft !== savedSummary;

  const startEdit = () => {
    setDraft(savedSummary);
    setEditing(true);
  };
  const cancelEdit = async () => {
    if (dirty) {
      const ok = await ask("編集内容は破棄されます。よろしいですか?", {
        title: "編集を破棄",
        kind: "warning",
        okLabel: "破棄",
        cancelLabel: "編集に戻る",
      });
      if (!ok) return;
    }
    setEditing(false);
    setDraft(savedSummary);
  };
  const saveEdit = async () => {
    if (!minutesId || saving) return;
    setSaving(true);
    try {
      const next = await updateMinutesSummary(minutesId, draft);
      showToast({ kind: "ok", text: "要約を更新しました" });
      setEditing(false);
      onSaved?.(next);
    } catch (e) {
      showToast({
        kind: "err",
        text: `保存失敗: ${e instanceof Error ? e.message : "不明なエラー"}`,
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="anim-fade-in flex flex-col h-full">
      {showBanner && (
        <SummaryStatusBanner
          job={job!}
          onCancel={onCancel}
          onRetry={() => onGenerate(undefined)}
        />
      )}

      {editing ? (
        <>
          <div className="flex items-center justify-between gap-2 px-5 py-1.5 border-b border-(--border) bg-(--surface)">
            <span className="text-[11px] text-(--t3)">
              編集中{dirty && <span className="text-(--accent) ml-1.5">●</span>}
            </span>
            <div className="flex items-center gap-1.5">
              <button
                onClick={cancelEdit}
                disabled={saving}
                className="btn btn-ghost h-7 px-2.5 text-[11px]"
              >
                破棄
              </button>
              <button
                onClick={saveEdit}
                disabled={saving || !dirty}
                className="btn h-7 px-3 text-[11px]"
                style={{ background: "var(--accent)", color: "white", border: "none" }}
              >
                {saving ? <Spinner size={11} /> : "保存"}
              </button>
            </div>
          </div>
          <div className="flex-1 min-h-0">
            <MarkdownEditor value={draft} onChange={setDraft} autoFocus />
          </div>
        </>
      ) : (
        <div className="flex-1 overflow-y-auto">
          {displayText ? (
            <div className="px-6 py-6">
              <MarkdownView
                value={displayText}
                searchQuery={searchQuery}
                activeMatchIndex={activeMatchIndex}
                onMatchesChange={onMatchesChange}
              />
              {savedSummary && !isRunning && (
                <SummaryFooter
                  savedModel={savedModel}
                  canAct={canAct}
                  busy={busy}
                  canEdit={canEdit}
                  onEdit={startEdit}
                  onGenerate={onGenerate}
                />
              )}
            </div>
          ) : (
            !isRunning && !isFailed && (
              <SummaryEmpty
                canAct={canAct}
                busy={busy}
                onGenerate={onGenerate}
              />
            )
          )}
        </div>
      )}
    </div>
  );
}

function SummaryStatusBanner({
  job, onCancel, onRetry,
}: {
  job: SummarizeStatus;
  onCancel: () => void;
  onRetry: () => void;
}) {
  const state = job.state;
  const providerLabel = job.provider
    ? PROVIDER_LABELS[job.provider as SummarizeProvider] || job.provider
    : "";
  const modelLabel = job.model ? ` / ${job.model}` : "";

  if (state === "queued" || state === "running") {
    const chars = (job.partial_text || "").length;
    const stageLabel = job.stage_label;
    const activity = job.activity || "";
    return (
      <div className="summary-banner summary-banner-running">
        <Spinner size={12} />
        <div className="flex-1 min-w-0 flex flex-col gap-0.5">
          <span className="text-[12px] text-(--t2) truncate">
            {stageLabel ? (
              <span className="font-medium text-(--t1)">{stageLabel}</span>
            ) : (
              "要約生成中"
            )}
            {providerLabel && (
              <span className="text-(--t3)"> · {providerLabel}{modelLabel}</span>
            )}
          </span>
          {activity && (
            <span className="text-[11px] text-(--t3) truncate" title={activity}>
              {activity}
            </span>
          )}
          {chars > 0 && (
            <span className="num text-[11px] text-(--t3) tabular-nums">
              {chars.toLocaleString()} chars 生成済み
            </span>
          )}
        </div>
        <button onClick={onCancel} className="btn h-7 px-2.5 text-[11px]">
          キャンセル
        </button>
      </div>
    );
  }
  if (state === "failed") {
    return (
      <div className="summary-banner summary-banner-error">
        <WarningCircle size={14} weight="regular" className="text-(--danger) shrink-0" />
        <div className="flex-1 min-w-0">
          <p className="text-[12px] text-(--danger) font-medium">
            要約生成に失敗しました{job.error_code ? ` [${job.error_code}]` : ""}
          </p>
          {job.error_message && (
            <p className="text-[11px] text-(--t3) mt-0.5 truncate">{job.error_message}</p>
          )}
        </div>
        <button onClick={onRetry} className="btn h-7 px-2.5 text-[11px]">
          再試行
        </button>
      </div>
    );
  }
  if (state === "skipped") {
    return (
      <div className="summary-banner summary-banner-info">
        <span className="text-[12px] text-(--t3) flex-1">
          {job.error_message || "発話が短すぎて要約をスキップしました"}
        </span>
      </div>
    );
  }
  if (state === "cancelled") {
    return (
      <div className="summary-banner summary-banner-info">
        <span className="text-[12px] text-(--t3) flex-1">
          要約生成がキャンセルされました
        </span>
        <button onClick={onRetry} className="btn h-7 px-2.5 text-[11px]">
          再生成
        </button>
      </div>
    );
  }
  return null;
}

function SummaryEmpty({
  canAct, busy, onGenerate,
}: {
  canAct: boolean;
  busy: boolean;
  onGenerate: (provider?: SummarizeProvider) => void;
}) {
  return (
    <div className="anim-fade-in flex flex-col items-center justify-center h-full gap-3 px-6">
      <p className="text-[12px] text-(--t3)">要約はまだ生成されていません</p>
      {canAct && (
        <SummaryGenerateButton busy={busy} onGenerate={onGenerate} primary />
      )}
    </div>
  );
}

function SummaryFooter({
  savedModel, canAct, busy, canEdit, onEdit, onGenerate,
}: {
  savedModel: string;
  canAct: boolean;
  busy: boolean;
  canEdit?: boolean;
  onEdit?: () => void;
  onGenerate: (provider?: SummarizeProvider) => void;
}) {
  // savedModel は "claude_api:claude-sonnet-4-6" のような形式 → 表示用に分解
  const [providerSlug, modelName] = savedModel.includes(":")
    ? savedModel.split(":", 2)
    : [savedModel, ""];
  const providerLabel = providerSlug
    ? (PROVIDER_LABELS[providerSlug as SummarizeProvider] || providerSlug)
    : "";

  return (
    <div className="summary-footer">
      <span className="text-[11px] text-(--t3) flex-1 truncate">
        {providerLabel && <>{providerLabel}{modelName ? ` · ${modelName}` : ""}</>}
        {!providerLabel && "要約モデル不明"}
      </span>
      {canEdit && onEdit && (
        <button
          onClick={onEdit}
          title="要約を編集"
          className="btn btn-ghost h-7 px-2.5 text-[11px] flex items-center gap-1.5"
        >
          <PencilSimple size={12} weight="regular" />
          編集
        </button>
      )}
      {canAct && (
        <SummaryGenerateButton
          busy={busy}
          onGenerate={onGenerate}
          label="再要約"
          icon={<ArrowsClockwise size={12} weight="regular" />}
          dropUp
        />
      )}
    </div>
  );
}

function SummaryGenerateButton({
  busy, onGenerate, primary, label, icon, dropUp,
}: {
  busy: boolean;
  onGenerate: (provider?: SummarizeProvider) => void;
  primary?: boolean;
  label?: string;
  icon?: React.ReactNode;
  dropUp?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const popoverRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      const t = e.target as Node;
      if (popoverRef.current && !popoverRef.current.contains(t)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  const buttonLabel = label || "要約を生成";
  const buttonIcon = icon || <MagicWand size={12} weight="regular" />;

  return (
    <div ref={popoverRef} className="relative inline-flex items-center">
      <button
        onClick={() => onGenerate(undefined)}
        disabled={busy}
        className={`btn h-8 ${primary ? "btn-primary" : "btn-ghost"} text-[11px] pl-3 pr-2 rounded-r-none`}
        style={{ borderTopRightRadius: 0, borderBottomRightRadius: 0 }}
        title="既定のproviderで実行"
      >
        {busy ? <Spinner size={12} /> : buttonIcon}
        <span>{buttonLabel}</span>
      </button>
      <button
        onClick={() => setOpen((v) => !v)}
        disabled={busy}
        className={`btn h-8 ${primary ? "btn-primary" : "btn-ghost"} text-[11px] px-1.5 border-l border-(--accent-line) rounded-l-none`}
        style={{ borderTopLeftRadius: 0, borderBottomLeftRadius: 0, marginLeft: -1 }}
        title="provider を選択"
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <CaretDown size={11} weight="bold" />
      </button>

      {open && (
        <div
          role="menu"
          className={`anim-fade-in absolute right-0 min-w-[200px] dialog-shell py-1 z-30 ${
            dropUp ? "bottom-full mb-1" : "top-full mt-1"
          }`}
        >
          {SUMMARIZE_PROVIDERS.map((p) => (
            <button
              key={p}
              type="button"
              onClick={() => { setOpen(false); onGenerate(p); }}
              className="w-full text-left px-3 py-1.5 text-[12px] cursor-pointer flex items-center gap-2 transition-colors hover:bg-(--surface-2) text-(--t2) hover:text-(--t1)"
              style={{ background: "transparent", border: "none" }}
            >
              <span className="flex-1 truncate">{PROVIDER_LABELS[p]}</span>
              {CLOUD_PROVIDERS.includes(p) && (
                <span className="text-[10px] text-(--t4)">cloud</span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

interface Match { segIndex: number; start: number; end: number; }

function SegmentText({
  text, matches, allMatches, activeIndex, activeId,
}: {
  text: string;
  matches: Match[];
  allMatches: Match[];
  activeIndex: number;
  activeId: string;
}) {
  if (matches.length === 0) return <>{text}</>;
  const pieces: React.ReactNode[] = [];
  let cursor = 0;
  matches.forEach((m, i) => {
    if (m.start > cursor) pieces.push(<span key={`p${i}`}>{text.slice(cursor, m.start)}</span>);
    const globalIdx = allMatches.indexOf(m);
    const isActive = globalIdx === activeIndex;
    pieces.push(
      <mark
        key={`m${i}`}
        id={isActive ? activeId : undefined}
        className={`find-mark ${isActive ? "is-active" : ""}`}
      >
        {text.slice(m.start, m.end)}
      </mark>
    );
    cursor = m.end;
  });
  if (cursor < text.length) pieces.push(<span key="tail">{text.slice(cursor)}</span>);
  return <>{pieces}</>;
}

function FindBar({
  query, onChange, inputRef, matches, index,
  otherCount, activeTab,
  onJumpSummary, onJumpTranscript,
  onPrev, onNext, onClose,
}: {
  query: string;
  onChange: (v: string) => void;
  inputRef: React.MutableRefObject<HTMLInputElement | null>;
  matches: number;
  index: number;
  otherCount: number;
  activeTab: "summary" | "transcript";
  onJumpSummary: () => void;
  onJumpTranscript: () => void;
  onPrev: () => void;
  onNext: () => void;
  onClose: () => void;
}) {
  const otherLabel = activeTab === "transcript" ? "要約" : "本文";
  const onJumpOther = activeTab === "transcript" ? onJumpSummary : onJumpTranscript;

  const { imeHandlers, isImeEnter } = useImeSafeEnter();
  return (
    <div className="find-bar" {...imeHandlers}>
      <MagnifyingGlass size={12} weight="regular" className="text-(--t3) shrink-0" />
      <input
        ref={inputRef}
        value={query}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            if (isImeEnter(e)) return;
            e.preventDefault();
            if (e.shiftKey) onPrev(); else onNext();
          } else if (e.key === "Escape") {
            e.preventDefault();
            onClose();
          }
        }}
        placeholder={activeTab === "transcript" ? "本文を検索" : "要約を検索"}
        className="find-bar-input"
        aria-label={activeTab === "transcript" ? "本文を検索" : "要約を検索"}
      />
      <span className="num text-[11px] text-(--t3) shrink-0">
        {matches === 0 ? "0/0" : `${index}/${matches}`}
      </span>
      {query && otherCount > 0 && (
        <button
          onClick={onJumpOther}
          className="text-[10px] text-(--accent) hover:underline shrink-0"
          style={{ background: "transparent", border: "none", cursor: "pointer" }}
          title={`${otherLabel}に ${otherCount} 件`}
        >
          {otherLabel}: {otherCount}
        </button>
      )}
      <div className="flex items-center gap-0.5 shrink-0">
        <button onClick={onPrev} disabled={matches === 0} className="icon-btn !w-6 !h-6" title="前 (Shift+Enter)">
          <CaretUp size={11} weight="bold" />
        </button>
        <button onClick={onNext} disabled={matches === 0} className="icon-btn !w-6 !h-6" title="次 (Enter)">
          <CaretDown size={11} weight="bold" />
        </button>
        <button onClick={onClose} className="icon-btn !w-6 !h-6" title="閉じる (Esc)">
          <X size={11} weight="bold" />
        </button>
      </div>
    </div>
  );
}

/* ── 話者ごとの安定色 (低彩度のミュート系パレットを循環) ── */
const _speakerColorCache = new Map<string, string>();
const SPEAKER_PALETTE = [
  "hsl(210, 12%, 52%)", // slate
  "hsl(35, 16%, 55%)",  // tan
  "hsl(150, 10%, 50%)", // sage
  "hsl(265, 10%, 56%)", // muted purple
  "hsl(0, 0%, 48%)",    // gray
];
function speakerColor(key: string | null | undefined): string {
  const k = key || "?";
  const cached = _speakerColorCache.get(k);
  if (cached) return cached;
  let hash = 5381;
  for (let i = 0; i < k.length; i++) {
    hash = ((hash << 5) + hash + k.charCodeAt(i)) | 0;
  }
  const idx = Math.abs(hash) % SPEAKER_PALETTE.length;
  const color = SPEAKER_PALETTE[idx];
  _speakerColorCache.set(k, color);
  return color;
}

function TranscriptMinimap({
  segments, scrollRef, matches, findIndex, onSegmentClick,
}: {
  segments: TranscriptSegment[];
  scrollRef: React.MutableRefObject<HTMLDivElement | null>;
  matches: Match[];
  findIndex: number;
  onSegmentClick: (segIndex: number) => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [scrollHeight, setScrollHeight] = useState(1);
  const [clientHeight, setClientHeight] = useState(1);
  const [dragging, setDragging] = useState(false);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const update = () => {
      setScrollTop(el.scrollTop);
      setScrollHeight(el.scrollHeight);
      setClientHeight(el.clientHeight);
    };
    update();
    el.addEventListener("scroll", update, { passive: true });
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => {
      el.removeEventListener("scroll", update);
      ro.disconnect();
    };
  }, [scrollRef, segments.length]);

  const scrollToY = (clickY: number, smooth = false) => {
    const rect = containerRef.current?.getBoundingClientRect();
    const scroller = scrollRef.current;
    if (!rect || !scroller) return;
    const ratio = Math.max(0, Math.min(1, (clickY - rect.top) / rect.height));
    const target = ratio * scrollHeight - clientHeight / 2;
    scroller.scrollTo({ top: target, behavior: smooth ? "smooth" : "auto" });
  };

  const handlePointerDown = (e: React.PointerEvent) => {
    e.preventDefault();
    setDragging(true);
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    scrollToY(e.clientY, false);
  };
  const handlePointerMove = (e: React.PointerEvent) => {
    if (!dragging) return;
    scrollToY(e.clientY, false);
  };
  const handlePointerUp = (e: React.PointerEvent) => {
    setDragging(false);
    (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
    // クリックの場合、対応するセグメントを center にスクロール
    const rect = containerRef.current?.getBoundingClientRect();
    if (rect) {
      const ratio = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
      const segIdx = Math.min(segments.length - 1, Math.floor(ratio * segments.length));
      onSegmentClick(segIdx);
    }
  };

  const indicatorTop = (scrollTop / Math.max(1, scrollHeight)) * 100;
  const indicatorHeight = (clientHeight / Math.max(1, scrollHeight)) * 100;

  // 検索マッチ位置の dot を集約
  const matchSegSet = useMemo(() => {
    const s = new Set<number>();
    matches.forEach((m) => s.add(m.segIndex));
    return s;
  }, [matches]);
  const activeMatchSegIdx = matches.length > 0 ? matches[Math.max(0, Math.min(findIndex, matches.length - 1))]?.segIndex : null;

  return (
    <div
      ref={containerRef}
      className="transcript-minimap"
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
    >
      <div className="minimap-bars">
        {segments.map((seg, i) => (
          <div
            key={i}
            className="minimap-bar"
            style={{ background: speakerColor(seg.speaker_id || seg.speaker_label) }}
            title={`${seg.speaker_label || "?"}`}
          />
        ))}
      </div>
      {matchSegSet.size > 0 && (
        <div className="minimap-matches">
          {Array.from(matchSegSet).map((i) => (
            <div
              key={i}
              className={`minimap-match ${i === activeMatchSegIdx ? "is-active" : ""}`}
              style={{ top: `${(i / Math.max(1, segments.length)) * 100}%` }}
            />
          ))}
        </div>
      )}
      <div
        className="minimap-viewport"
        style={{
          top: `${indicatorTop}%`,
          height: `${Math.max(2, indicatorHeight)}%`,
        }}
      />
    </div>
  );
}
