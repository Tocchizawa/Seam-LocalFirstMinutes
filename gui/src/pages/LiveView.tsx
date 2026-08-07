import { useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowsIn } from "@phosphor-icons/react";
import { useRecording, type StreamStatus } from "../lib/recording-context";
import { Spinner } from "../components/Spinner";
import { ModelDownloadProgress } from "../components/ModelDownloadProgress";

function fmtTs(s: number) {
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
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
    liveSegments, streamStatus, recording, modelLoadLog, modelDownload,
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

  return (
    <div className="relative h-full flex flex-col overflow-hidden">
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
            {liveSegments.map((seg, i) => (
              <div key={i} className="flat-row" style={{ cursor: "default" }}>
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
          className="absolute right-16 z-20 anim-fade-in flex items-center gap-1.5 h-8 px-3 rounded-full text-[11px] font-medium shadow-md cursor-pointer transition-transform hover:scale-105 bottom-4"
          style={{
            background: "var(--accent)",
            color: "white",
            border: "none",
          }}
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
          className="absolute right-4 bottom-4 z-20 flex items-center justify-center h-9 w-9 rounded-full shadow-md cursor-pointer transition-transform hover:scale-105"
          style={{
            background: "var(--surface)",
            border: "1px solid var(--border)",
            color: "var(--t1)",
          }}
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
  const speed = status.avg_speed_ratio ?? 0;
  const pending = status.pending_audio_sec ?? 0;
  const cur = status.current_chunk_audio_sec ?? 0;
  const proc = status.current_processing_sec ?? 0;
  const queueSize = status.queue_size ?? 0;
  const totalSegments = status.total_segments ?? 0;

  return (
    <div className="flex items-center gap-4 px-5 py-1.5 border-b border-(--border) text-[10px] text-(--t3) num">
      {isProcessing ? (
        <span className="flex items-center gap-1.5 text-(--accent)">
          <span className="status-dot status-dot--pulse" />
          {cur.toFixed(1)}秒の音声を処理中 · {proc.toFixed(1)}秒経過
        </span>
      ) : (
        <span className="text-(--t4)">待機中</span>
      )}
      <span className="ml-auto flex items-center gap-3">
        {speed > 0 && <span title="直近の処理速度">速度 ×{speed.toFixed(1)}</span>}
        {pending > 0 && <span title="未処理の音声合計">未処理 {pending.toFixed(1)}秒</span>}
        <span title="キューサイズ">
          待機中 {queueSize}{status.queue_maxsize ? `/${status.queue_maxsize}` : ""}
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
        <span>確定 {totalSegments}件</span>
      </span>
    </div>
  );
}
