import type { StreamStatus } from "../lib/recording-context";

export function StreamStatusBar({ status }: { status: StreamStatus }) {
  const isProcessing = (status.current_chunk_audio_sec ?? 0) > 0;
  const pending = status.pending_audio_sec ?? 0;
  const elapsed = status.current_processing_sec ?? 0;
  if (!isProcessing && !status.queue_size && !pending && !status.dropped_chunks && !status.transcribe_errors) return null;

  return (
    <div className="stream-status-bar num">
      {isProcessing ? (
        <span className="stream-status-active">
          <span className="status-dot" />
          文字起こし中 · {Math.round(elapsed)}秒
        </span>
      ) : (
        <span className="stream-status-idle">文字起こし</span>
      )}
      <span className="stream-status-metrics">
        {pending > 0 && (
          <span title="文字起こしを待っている音声">処理待ち {pending.toFixed(0)}秒</span>
        )}
        <span title="文字起こし待ちの音声チャンク数">
          待機中 {status.queue_size}{status.queue_maxsize ? `/${status.queue_maxsize}` : ""}件
        </span>
        {status.dropped_chunks ? (
          <span className="stream-status-warning" title="過負荷で破棄した音声">
            未処理の音声 {status.dropped_chunks}件 · {(status.dropped_audio_sec ?? 0).toFixed(1)}秒
          </span>
        ) : null}
        {status.transcribe_errors ? (
          <span className="stream-status-error" title="文字起こし失敗回数">
            文字起こしエラー {status.transcribe_errors}件
          </span>
        ) : null}
        <span>文字起こし済み {status.total_segments}件</span>
      </span>
    </div>
  );
}
