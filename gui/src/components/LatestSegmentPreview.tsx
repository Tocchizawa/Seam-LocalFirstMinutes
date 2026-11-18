import { ArrowsOut } from "@phosphor-icons/react";
import { useRecording } from "../lib/recording-context";
import { ModelDownloadProgress } from "./ModelDownloadProgress";

interface Props {
  onExpand: () => void;
}

function fmtTs(s: number) {
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}

/**
 * 録音中、ステータス表示の下に「最新発話 1 行」を表示するプレビュー。
 * - 新しい発話が来たら fade-in で入れ替え
 * - 1 行 truncate
 * - 右端の expand アイコンで全画面ライブビューへ
 */
export function LatestSegmentPreview({ onExpand }: Props) {
  const {
    liveSegments, recording, streamStatus, modelDownload, transcriptionDeferred,
  } = useRecording();

  const latest = liveSegments.length > 0
    ? liveSegments[liveSegments.length - 1]
    : null;

  if (!recording) return null;

  return (
    <div
      className="latest-segment-preview flex items-center gap-2 px-5 py-2 border-b border-(--border)"
    >
      <div className="flex-1 min-w-0 overflow-hidden">
        {latest ? (
          <div
            key={`${latest.session_id}:${latest.start}:${latest.end}`}
            className="anim-fade-in flex items-baseline gap-2 min-w-0"
          >
            <span className="num text-[10px] text-(--t3) shrink-0">
              {fmtTs(latest.start)}
            </span>
            <span className="text-[12px] text-(--t1) truncate min-w-0 flex-1">
              {latest.text}
            </span>
          </div>
        ) : transcriptionDeferred ? (
          <span className="text-(--t2)">停止後に文字起こし</span>
        ) : modelDownload?.state === "downloading" ? (
          <ModelDownloadProgress status={modelDownload} compact />
        ) : streamStatus?.model_state === "loading" ? (
          <span className="text-[11px] text-(--t2) truncate min-w-0 flex-1">
            音声認識を準備中
          </span>
        ) : streamStatus?.model_state === "error" ? (
          <span className="text-[11px] text-(--danger) truncate min-w-0 flex-1">
            音声認識を開始できませんでした
          </span>
        ) : (
          <span className="text-[11px] text-(--t4)">会話の音声を待っています</span>
        )}
      </div>
      <button
        type="button"
        onClick={onExpand}
        className="icon-btn shrink-0"
        title="ライブ文字起こしを表示"
        aria-label="ライブ文字起こしを表示"
      >
        <ArrowsOut size={16} />
      </button>
    </div>
  );
}
