import { useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowsIn } from "@phosphor-icons/react";
import { useRecording, type StreamStatus } from "../lib/recording-context";
import { Spinner } from "../components/Spinner";
import { ModelDownloadProgress } from "../components/ModelDownloadProgress";

function fmtTs(s: number) {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
    : `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

interface Props {
  /**
   * `streamStatusBar` true なら現在の StreamStatus を上部に表示する
   * (MainView 内で使う時用; 単独 Live ページではバナーがあるので不要)。
   */
  showStreamStatusBar?: boolean;
  /** 縮小ボタンで呼ぶ。省略時は表示しない。 */
  onClose?: () => void;
}

/**
 * 録音中のリアルタイム文字起こしを表示する画面。
 * MainView の旧 LiveTranscript と等価だが、context の liveSegments を読むだけ。
 */
export function LiveView({ showStreamStatusBar = false, onClose }: Props) {
  const {
    liveSegments, streamStatus, recording, recordingStarting, recordingStopping,
    waitingForModel, paused, elapsedSec, modelLoadLog, modelDownload,
  } = useRecording();
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const stickToBottomRef = useRef(true);
  /** smooth スクロール中は途中の scroll で showJump が true に戻らないようにする */
  const jumpingToBottomRef = useRef(false);
  const [showJump, setShowJump] = useState(false);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distanceFromBottom = () => el.scrollHeight - (el.scrollTop + el.clientHeight);
    const syncJumpFromScrollPosition = () => {
      const near = distanceFromBottom() < 80;
      stickToBottomRef.current = near;
      setShowJump(!near);
    };
    const onScroll = () => {
      const near = distanceFromBottom() < 80;
      if (jumpingToBottomRef.current) {
        if (near) jumpingToBottomRef.current = false;
        else return;
      }
      syncJumpFromScrollPosition();
    };
    /** smooth ジャンプをユーザーが止めたとき、jumping フラグだけ残るのを防ぐ */
    const onScrollEnd = () => {
      if (!jumpingToBottomRef.current) return;
      jumpingToBottomRef.current = false;
      syncJumpFromScrollPosition();
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    el.addEventListener("scrollend", onScrollEnd);
    return () => {
      el.removeEventListener("scroll", onScroll);
      el.removeEventListener("scrollend", onScrollEnd);
    };
  }, []);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (stickToBottomRef.current) el.scrollTop = el.scrollHeight;
  }, [liveSegments.length]);

  const jumpToBottom = () => {
    const el = scrollRef.current;
    if (!el) return;
    jumpingToBottomRef.current = true;
    stickToBottomRef.current = true;
    setShowJump(false);
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  };

  const elapsed = fmtTs(elapsedSec);

  return (
    <div className="relative h-full flex flex-col overflow-hidden">
      <header className="live-view-head">
        <div className="min-w-0">
          <div className="live-view-title-line">
            <span className={`status-dot ${(recordingStarting || (recording && !paused && !recordingStopping)) ? "status-dot--pulse" : ""}`} />
            <h1>ライブ文字起こし</h1>
          </div>
          <p aria-live="polite">
            {recordingStopping ? "録音を保存しています"
              : recordingStarting ? waitingForModel ? "ローカル処理の完了を待っています" : "録音を準備しています"
                : recording ? paused ? "録音を一時停止中" : "会話を記録しています"
                  : "録音は停止しています"}
          </p>
        </div>
        <div className="live-view-meta num" aria-live="polite">
          <span>{elapsed}</span>
          <span>{liveSegments.length} 件</span>
        </div>
      </header>
      {showStreamStatusBar && streamStatus && (
        <StreamStatusBar status={streamStatus} />
      )}

      <div ref={scrollRef} className="flex-1 overflow-y-auto">
        {liveSegments.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center gap-2 px-6 text-center">
            {recording ? (
              <LiveWaitingState
                streamStatus={streamStatus}
                modelLoadLog={modelLoadLog}
                modelDownload={modelDownload}
              />
            ) : (
              <p className="text-[11px] text-(--t3)">録音されていません</p>
            )}
          </div>
        ) : (
          <div className="anim-fade-in">
            {liveSegments.map((seg) => (
              <div key={`${seg.session_id ?? "live"}-${seg.start}-${seg.end}-${seg.speaker_id ?? ""}-${seg.text}`} className="flat-row live-transcript-row" style={{ cursor: "default" }}>
                <span
                  className="num text-[10px] text-(--t3) shrink-0 min-w-[48px]"
                  style={{ fontWeight: 400 }}
                >
                  {fmtTs(seg.start)}
                </span>
                <span className="text-[13px] leading-relaxed text-(--t1) flex-1">
                  {seg.text}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      {showJump && (
        <button
          type="button"
          onClick={jumpToBottom}
          title="最新へ"
          className="live-jump-btn absolute right-16 z-20 anim-fade-in flex items-center gap-1.5 h-9 px-3 rounded-lg text-xs font-medium cursor-pointer bottom-4"
        >
          <ArrowDown size={11} weight="bold" />
          最新へ
        </button>
      )}

      {onClose && (
        <button
          type="button"
          onClick={onClose}
          title="縮小して一覧へ戻る"
          className="live-close-btn absolute right-4 bottom-4 z-20 flex items-center justify-center h-9 w-9 rounded-lg cursor-pointer"
        >
          <ArrowsIn size={14} weight="regular" />
        </button>
      )}
    </div>
  );
}

function LiveWaitingState({
  streamStatus,
  modelLoadLog,
  modelDownload,
}: {
  streamStatus: StreamStatus | null;
  modelLoadLog: string | null;
  modelDownload: import("../lib/api").WhisperDownloadStatus | null;
}) {
  const modelState = streamStatus?.model_state;
  const modelError = streamStatus?.model_error;

  if (modelDownload?.state === "downloading") {
    return (
      <div className="w-full max-w-xs">
        <ModelDownloadProgress status={modelDownload} />
      </div>
    );
  }

  if (modelDownload?.state === "error") {
    return (
      <div className="w-full max-w-xs">
        <ModelDownloadProgress status={modelDownload} />
        {modelError && <p className="mt-1 text-[10px] text-(--t4) truncate">{modelError}</p>}
      </div>
    );
  }

  if (modelState === "loading") {
    return (
      <div className="flex items-center gap-3">
        <Spinner size={12} />
        <div className="flex flex-col items-start gap-0.5">
          <p className="text-[11px] text-(--t2)">Whisper モデルを読み込み中</p>
          <p className="text-[10px] text-(--t4)" title={modelLoadLog ?? undefined}>
            {modelDownload?.state === "ready"
              ? "キャッシュ済みモデルをメモリに展開しています"
              : "モデルのキャッシュを確認しています"}
          </p>
        </div>
      </div>
    );
  }
  if (modelState === "error") {
    return (
      <>
        <p className="text-[11px] text-(--danger)">モデルのロードに失敗しました</p>
        {modelError && (
          <p className="text-[10px] text-(--t4) max-w-[400px] truncate">{modelError}</p>
        )}
      </>
    );
  }
  // モデル ready かつまだ発話なし
  return (
    <>
      <Spinner size={12} />
      <p className="text-[10px] text-(--t3)">発話を待機中...</p>
    </>
  );
}

function StreamStatusBar({
  status,
}: {
  status: StreamStatus;
}) {
  const isProcessing = (status.current_chunk_audio_sec ?? 0) > 0;
  const pending = status.pending_audio_sec ?? 0;
  const proc = status.current_processing_sec ?? 0;
  const queueSize = status.queue_size ?? 0;
  const totalSegments = status.total_segments ?? 0;

  return (
    <div className="stream-status-bar num" role="status" aria-live="polite">
      {isProcessing ? (
        <span className="stream-status-active">
          <span className="status-dot status-dot--pulse" />
          音声を処理中 · {proc.toFixed(1)}秒経過
        </span>
      ) : (
        <span className="stream-status-idle">発話を待機中</span>
      )}
      <span className="stream-status-metrics">
        {pending > 0 && <span title="未処理の音声合計">未処理 {pending.toFixed(0)}秒</span>}
        <span title="文字起こし待ちの音声チャンク数">
          キュー {queueSize}{status.queue_maxsize ? `/${status.queue_maxsize}` : ""}件
        </span>
        {status.dropped_chunks ? (
          <span className="stream-status-warning" title="過負荷で破棄した音声">
            音声欠落 {status.dropped_chunks}件 ({(status.dropped_audio_sec ?? 0).toFixed(1)}秒)
          </span>
        ) : null}
        {status.transcribe_errors ? (
          <span className="stream-status-error" title="文字起こし失敗回数">
            失敗 {status.transcribe_errors}
          </span>
        ) : null}
        <span>確定 {totalSegments}件</span>
      </span>
    </div>
  );
}
