import { useEffect, useState } from "react";
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
    liveSegments, recording, streamStatus, modelDownload,
  } = useRecording();

  const latest = liveSegments.length > 0
    ? liveSegments[liveSegments.length - 1]
    : null;

  // key を increment して再マウントで fade-in を再生させる
  const [animKey, setAnimKey] = useState(0);
  const [displayed, setDisplayed] = useState(latest);

  useEffect(() => {
    if (latest === displayed) return;
    setDisplayed(latest);
    setAnimKey((k) => k + 1);
  }, [latest, displayed]);

  if (!recording) return null;

  return (
    <div
      className="flex items-center gap-2 px-4 py-1.5 border-b border-(--border)"
      style={{ background: "var(--surface)" }}
    >
      <div className="flex-1 min-w-0 overflow-hidden">
        {displayed ? (
          <div
            key={animKey}
            className="anim-fade-in flex items-baseline gap-2 min-w-0"
          >
            <span className="num text-[10px] text-(--t3) shrink-0">
              {fmtTs(displayed.start)}
            </span>
            <span className="text-[12px] text-(--t1) truncate min-w-0 flex-1">
              {displayed.text}
            </span>
          </div>
        ) : modelDownload?.state === "downloading" ? (
          <ModelDownloadProgress status={modelDownload} compact />
        ) : streamStatus?.model_state === "loading" ? (
          <span className="text-[11px] text-(--t2) truncate min-w-0 flex-1">
            Whisperモデルを読み込み中
            <span className="text-(--t4)"> · キャッシュを確認しています</span>
          </span>
        ) : streamStatus?.model_state === "error" ? (
          <span className="text-[11px] text-(--danger) truncate min-w-0 flex-1">
            モデルのロードに失敗
          </span>
        ) : (
          <span className="text-[11px] text-(--t4)">発話を待機中...</span>
        )}
      </div>
      <button
        type="button"
        onClick={onExpand}
        className="icon-btn shrink-0"
        title="全画面ライブを表示"
      >
        <ArrowsOut size={12} weight="regular" />
      </button>
    </div>
  );
}
