import type { StreamStatus } from "../lib/recording-context";

export function StreamStatusBar({ status }: { status: StreamStatus }) {
  const isProcessing = (status.current_chunk_audio_sec ?? 0) > 0;
  const pending = status.pending_audio_sec ?? 0;
  const elapsed = status.current_processing_sec ?? 0;

  return (
    <div className="stream-status-bar num" role="status" aria-live="polite">
      {isProcessing ? (
        <span className="stream-status-active">
          <span className="status-dot status-dot--pulse" />
          文字起こし中 · 経過 {elapsed.toFixed(1)}秒
        </span>
      ) : (
        <span className="stream-status-idle">発話を待機中</span>
      )}
      <span className="stream-status-metrics">
        {pending > 0 && (
          <span title="未処理の音声合計">未処理 {pending.toFixed(0)}秒</span>
        )}
        <span title="文字起こし待ちの音声チャンク数">
          キュー {status.queue_size}{status.queue_maxsize ? `/${status.queue_maxsize}` : ""}件
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
        <span>確定 {status.total_segments}件</span>
      </span>
    </div>
  );
}
