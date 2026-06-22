import { useCallback } from "react";
import { Microphone, Stop, Pause, Play } from "@phosphor-icons/react";
import { useRecording } from "../lib/recording-context";
import { stopRecording } from "../lib/api";

function fmt(s: number) {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
    : `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

interface Props {
  isOnLive: boolean;
  onBackToList: () => void;
}

/**
 * 録音中に main 以外のビュー (Detail / Live) で常時表示するバナー。
 * - ライブ画面への切り替え / 一覧へ戻る
 * - 一時停止 / 停止
 */
export function RecordingBanner({ isOnLive, onBackToList }: Props) {
  const {
    recording, paused, togglePaused, elapsedSec, level,
    setRecording, setActiveSessionId, liveSegments,
  } = useRecording();

  const handleStop = useCallback(async () => {
    try { await stopRecording(); } catch {}
    setRecording(false);
    setActiveSessionId(null);
  }, [setRecording, setActiveSessionId]);

  if (!recording) return null;

  const speakingDot = !paused && level > 0.02;

  return (
    <div
      className="flex items-center gap-3 px-4 py-2 border-b border-(--border)"
      style={{
        background: "var(--surface)",
        boxShadow: "0 1px 0 var(--border)",
      }}
    >
      <span
        className={`status-dot ${paused ? "" : "status-dot--pulse"} shrink-0`}
        style={{ background: paused ? "var(--t3)" : "var(--accent)" }}
        aria-hidden
      />
      <span className="num text-[13px] text-(--t1) shrink-0">
        {fmt(elapsedSec)}
      </span>
      <span className="text-[11px] text-(--t3) shrink-0">
        {paused ? "一時停止中" : speakingDot ? "Speaking" : "録音中"}
      </span>
      <span className="text-[11px] text-(--t4) shrink-0">
        · 確定 {liveSegments.length} 件
      </span>

      <div className="ml-auto flex items-center gap-2 shrink-0">
        {isOnLive && (
          <button
            type="button"
            onClick={onBackToList}
            className="btn h-7 px-3 text-[11px]"
            title="一覧に戻る"
          >
            一覧へ戻る
          </button>
        )}
        <button
          type="button"
          onClick={togglePaused}
          className="icon-btn"
          title={paused ? "再開" : "一時停止"}
        >
          {paused
            ? <Play size={12} weight="fill" />
            : <Pause size={12} weight="fill" />}
        </button>
        <button
          type="button"
          onClick={handleStop}
          className="icon-btn"
          title="録音停止"
          style={{ color: "var(--danger)" }}
        >
          <Stop size={12} weight="fill" />
        </button>
        <Microphone size={12} weight="regular" className="text-(--t4)" aria-hidden />
      </div>
    </div>
  );
}
