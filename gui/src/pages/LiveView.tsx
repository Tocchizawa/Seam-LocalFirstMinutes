import { useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowsIn } from "@phosphor-icons/react";
import { useRecording } from "../lib/recording-context";
import { Spinner } from "../components/Spinner";
import { ModelDownloadProgress } from "../components/ModelDownloadProgress";
import { StreamStatusBar } from "../components/StreamStatusBar";
import type { StreamStatus } from "../lib/recording-context";

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
    liveSegments, streamStatus, recording, transcriptionDeferred, modelLoadLog, modelDownload,
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
  }, [liveSegments]);

  const jumpToBottom = () => {
    const el = scrollRef.current;
    if (!el) return;
    const animate = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    jumpingToBottomRef.current = animate;
    stickToBottomRef.current = true;
    setShowJump(false);
    el.scrollTo({ top: el.scrollHeight, behavior: animate ? "smooth" : "instant" });
  };

  return (
    <section className="relative h-full flex flex-col overflow-hidden">
      <header className="live-view-head">
        <h2>ライブ文字起こし</h2>
        <span className="num text-(--t3)">{liveSegments.length} 件</span>
        {onClose && (
          <button type="button" onClick={onClose} className="icon-btn" aria-label="ライブ文字起こしを閉じる" title="閉じる">
            <ArrowsIn size={16} />
          </button>
        )}
      </header>
      {showStreamStatusBar && streamStatus && (
        <StreamStatusBar status={streamStatus} />
      )}

      <div ref={scrollRef} className="live-transcript flex-1 overflow-y-auto">
        {liveSegments.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center gap-2 px-6 text-center">
            {transcriptionDeferred ? (
              <p className="text-(--t2)">録音の停止後に文字起こしします</p>
            ) : recording ? (
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
          <div className="transcript-sheet">
            {liveSegments.map((seg) => (
              <div key={`${seg.session_id ?? "live"}-${seg.start}-${seg.end}-${seg.speaker_id ?? ""}-${seg.text}`} className="flat-row live-transcript-row" style={{ cursor: "default" }}>
                <span
                  className="transcript-time num"
                  style={{ fontWeight: 400 }}
                >
                  {fmtTs(seg.start)}
                </span>
                <span className="transcript-copy">
                  {seg.speaker_label && <span className="transcript-speaker">{seg.speaker_label}</span>}
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
          className="live-jump-btn absolute right-4 z-20 anim-fade-in flex items-center gap-1.5 h-9 px-3 rounded-lg text-xs font-medium cursor-pointer bottom-4"
        >
          <ArrowDown size={11} weight="bold" />
          最新へ
        </button>
      )}

    </section>
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
          <p className="text-[11px] text-(--t2)">音声認識を準備しています</p>
          <p className="text-[10px] text-(--t4)" title={modelLoadLog ?? undefined}>
            {modelDownload?.state === "ready"
              ? "音声認識の準備をしています"
              : "音声認識モデルを確認しています"}
          </p>
        </div>
      </div>
    );
  }
  if (modelState === "error") {
    return (
      <>
        <p className="text-[11px] text-(--danger)">音声認識を開始できませんでした</p>
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
      <p className="text-[10px] text-(--t3)">会話の音声を待っています</p>
    </>
  );
}
