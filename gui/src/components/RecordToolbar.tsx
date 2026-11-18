import {
  Microphone, MicrophoneSlash, Stop, Pause, Play, SlidersHorizontal, X,
} from "@phosphor-icons/react";
import { Waveform } from "./Waveform";
import { useRecording } from "../lib/recording-context";
import type { Project } from "../lib/api";
import { showToast } from "../lib/toast";
import { Spinner } from "./Spinner";

interface Props {
  project: Project;
  onStopped: () => void;
  onOpenDeviceSettings: () => void;
}

function formatTime(seconds: number) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor(seconds / 60) % 60;
  const s = seconds % 60;
  const clock = `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return h ? `${h}:${clock}` : clock;
}

export function RecordToolbar({ project, onStopped, onOpenDeviceSettings }: Props) {
  const {
    recording, recordingStarting, recordingStopping, waitingForModel,
    paused, togglePaused, micMuted, toggleMicMuted, elapsedSec, level,
    startRecordingSession, cancelRecordingStart, stopRecordingSession,
  } = useRecording();
  const busy = recordingStarting || recordingStopping;
  const isLive = recording && !paused && !busy;
  const stateLabel = recordingStopping ? "録音を保存中"
    : recordingStarting ? waitingForModel ? "音声認識モデルが空くのを待っています" : "録音を準備中"
    : paused ? "一時停止中" : "録音中";
  const actionLabel = recordingStopping ? "保存中"
    : recordingStarting ? "キャンセル"
    : recording ? "停止" : "録音開始";

  const handleAction = async () => {
    if (recordingStarting) {
      cancelRecordingStart();
      return;
    }
    try {
      if (recording) {
        await stopRecordingSession();
        onStopped();
      } else {
        await startRecordingSession(project.id);
      }
    } catch (error) {
      showToast({ kind: "err", text: error instanceof Error ? error.message : "録音を操作できませんでした" });
    }
  };

  return (
    <header className="workspace-toolbar" data-recording={recording || undefined}>
      <h1 className="workspace-project truncate" title={project.name}>{project.name}</h1>
      {(recording || busy) && (
        <div className="recording-meter">
          <div className="recording-caption" title={stateLabel}>
            {isLive && <span className="status-dot recorder-dot" aria-hidden />}
            <span role="status" className="truncate">{stateLabel}</span>
          </div>
          {recording && <time className="num">{formatTime(elapsedSec)}</time>}
          {isLive && <div className="recording-wave"><Waveform level={level} /></div>}
        </div>
      )}
      <div className="recording-actions">
        {recording && (
          <>
            <button type="button" className="icon-btn" title={micMuted ? "ミュートを解除" : "マイクをミュート"} aria-label="マイクをミュート" aria-pressed={micMuted} onClick={toggleMicMuted} disabled={busy}>
              {micMuted ? <MicrophoneSlash size={16} /> : <Microphone size={16} />}
            </button>
            <button type="button" className="icon-btn" title={paused ? "録音を再開" : "録音を一時停止"} aria-label={paused ? "録音を再開" : "録音を一時停止"} onClick={togglePaused} disabled={busy}>
              {paused ? <Play size={16} /> : <Pause size={16} />}
            </button>
          </>
        )}
        <button type="button" className="icon-btn" title="録音デバイス" aria-label="録音デバイス" onClick={onOpenDeviceSettings}>
          <SlidersHorizontal size={16} />
        </button>
        <button
          type="button"
          className={`btn recording-button ${recordingStarting ? "" : "btn-primary"}`}
          disabled={recordingStopping}
          aria-label={recordingStarting ? "録音開始をキャンセル" : recording ? "録音を停止" : "録音開始"}
          onClick={() => void handleAction()}
        >
          {recordingStopping ? <Spinner size={14} /> : recordingStarting ? <X size={16} /> : recording ? <Stop size={16} weight="fill" /> : <Microphone size={16} />}
          <span>{actionLabel}</span>
        </button>
      </div>
    </header>
  );
}
