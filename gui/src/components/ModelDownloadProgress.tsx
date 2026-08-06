import { CheckCircle, WarningCircle } from "@phosphor-icons/react";
import { Spinner } from "./Spinner";
import { formatSize } from "../lib/parse-hf-progress";
import type { WhisperDownloadStatus } from "../lib/api";

interface Props {
  status: WhisperDownloadStatus | null;
  modelLabel?: string;
  compact?: boolean;
}

export function ModelDownloadProgress({ status, modelLabel, compact = false }: Props) {
  if (!status || status.state === "idle") return null;

  if (status.state === "error") {
    return (
      <div className={`flex items-center gap-1.5 text-(--danger) ${compact ? "text-[10px]" : "text-[11px]"}`}>
        <WarningCircle size={compact ? 12 : 14} />
        <span className="truncate" title={status.error ?? undefined}>
          {modelLabel ?? status.model ?? "モデル"} のダウンロードに失敗しました
        </span>
      </div>
    );
  }

  if (status.state === "ready") {
    return (
      <div className={`flex items-center gap-1.5 text-(--success) ${compact ? "text-[10px]" : "text-[11px]"}`}>
        <CheckCircle size={compact ? 12 : 14} weight="fill" />
        <span>{modelLabel ?? status.model ?? "モデル"} のダウンロード完了</span>
      </div>
    );
  }

  const percent = typeof status.percent === "number"
    ? Math.max(0, Math.min(100, status.percent))
    : null;
  const current = status.current_bytes > 0 ? formatSize(status.current_bytes) : null;
  const total = status.total_bytes > 0 ? formatSize(status.total_bytes) : null;

  return (
    <div className={`flex flex-col gap-1.5 ${compact ? "text-[10px]" : "text-[11px]"}`}>
      <div className="flex items-center gap-1.5 text-(--accent)">
        <Spinner size={compact ? 11 : 13} color="var(--accent)" />
        <span className="truncate">
          {modelLabel ?? status.model ?? "モデル"} をダウンロード中
        </span>
        {percent !== null && <span className="ml-auto num shrink-0">{Math.round(percent)}%</span>}
      </div>
      <div
        className="h-1.5 w-full overflow-hidden rounded-full bg-(--surface-2)"
        role="progressbar"
        aria-label={`${modelLabel ?? status.model ?? "モデル"} のダウンロード進捗`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent ?? undefined}
      >
        <div
          className="h-full rounded-full bg-(--accent) transition-[width] duration-300"
          style={{ width: `${percent ?? 8}%` }}
        />
      </div>
      {(current || total) && (
        <span className="text-(--t3) num">
          {current ?? "0 B"}{total ? ` / ${total}` : ""}
        </span>
      )}
    </div>
  );
}
