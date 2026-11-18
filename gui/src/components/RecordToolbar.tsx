import { useCallback, useMemo, useState } from "react";
import {
  Microphone, MicrophoneSlash, Stop, Pause, Play, SlidersHorizontal,
} from "@phosphor-icons/react";
import { Waveform } from "./Waveform";
import {
  stopRecording, updateSettings, setMicMuted, getRecordingStatus,
} from "../lib/api";
import { useRecording } from "../lib/recording-context";
import type { Project } from "../lib/api";

interface Props {
  project: Project;
  onStopped: () => void;
  onOpenDeviceSettings: () => void;
}

function fmt(s: number) {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
    : `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

export function RecordToolbar({
  project, onStopped, onOpenDeviceSettings,
}: Props) {
  const {
    recording, setRecording, recordingStarting, setRecordingStarting, paused, togglePaused,
    recordingStopping, setRecordingStopping, waitingForModel,
    startRecordingSession, cancelRecordingStart,
    micMuted, setMicMutedLocal,
    elapsedSec, resetElapsed, level,
    resetLive, micDevice, captureSystem, setActiveSessionId,
  } = useRecording();
  const [error, setError] = useState("");

  const handleToggleMicMute = useCallback(async () => {
    const next = !micMuted;
    // 楽観的更新 + サーバへ反映 (失敗時はロールバック)
    setMicMutedLocal(next);
    try {
      await setMicMuted(next);
    } catch {
      setMicMutedLocal(!next);
    }
  }, [micMuted, setMicMutedLocal]);

  const handleStart = async () => {
    if (recordingStarting) return;
    setError("");
    setRecordingStarting(true);
    try {
      const started = await startRecordingSession(project.id, micDevice, captureSystem);
      void updateSettings({
        recording: {
          last_project_id: project.id,
          last_mic_device: micDevice,
          last_capture_system: captureSystem,
        },
      }).catch(() => {});
      resetLive();
      resetElapsed();
      setActiveSessionId(started.session_id);
      setRecording(true);
    } catch (e: any) {
      if (!(e instanceof DOMException && e.name === "AbortError")) {
        setError(e.message || "録音の開始に失敗しました");
      }
    }
  };

  const cancelStart = cancelRecordingStart;

  const handleStop = useCallback(async () => {
    if (recordingStopping) return;
    setRecordingStopping(true);
    setError("");
    try {
      const result = await stopRecording();
      if (result.session_id) setActiveSessionId(result.session_id);
      onStopped();
    } catch (e: any) {
      setRecordingStopping(false);
      setError(e.message || "録音を停止できませんでした");
      void getRecordingStatus().then((status) => {
        setRecording(status.recording);
        setRecordingStopping(Boolean(status.stopping));
        setActiveSessionId(status.session_id ?? null);
      }).catch(() => {});
    }
  }, [recordingStopping, setRecordingStopping, setActiveSessionId, onStopped]);

  const handleMainClick = recordingStarting
    ? cancelStart
    : recording ? handleStop : handleStart;
  const stateLabel = recordingStopping
    ? "停止中"
    : recordingStarting
      ? waitingForModel ? "処理待ち · クリックで取消" : "準備中 · クリックで取消"
      : recording ? paused ? "一時停止中" : "録音中" : "待機中";

  const display = useMemo(() => fmt(elapsedSec), [elapsedSec]);

  return (
    <div className="toolbar">
      {/* Left: timer + waveform */}
      <div className="flex items-center gap-3 flex-1 min-w-0">
        <div className="flex flex-col items-start shrink-0 min-w-[72px]">
          <span className={`num text-[22px] leading-none ${recording ? "text-(--t1)" : "text-(--t3)"}`}>
            <span>{display}</span>
          </span>
          <span className="flex items-center gap-2 mt-1 h-4 text-xs text-(--t3)" aria-live="polite">
            {recording || recordingStarting || recordingStopping ? (
              <>
                <span
                  className={`status-dot ${((recording && !paused && !recordingStopping) || recordingStarting) ? "status-dot--pulse" : ""}`}
                  style={{ background: ((recording && !paused && !recordingStopping) || recordingStarting) ? "var(--accent)" : "var(--t3)" }}
                />
                <span>{stateLabel}</span>
              </>
            ) : (
              <span className="text-(--t4)">{stateLabel}</span>
            )}
          </span>
        </div>
        <div className="flex-1 min-w-0">
          <Waveform level={recording && !paused ? level : 0} alive={recording && !paused} height={48} />
        </div>
      </div>

      {/* Right: mic mute + device + record/pause/stop */}
      <div className="flex items-center gap-3 shrink-0">
        {recording && (
          <button
            onClick={handleToggleMicMute}
            className="icon-btn"
            title={micMuted ? "マイクのミュート解除" : "マイクをミュート"}
            style={micMuted ? { color: "var(--danger)" } : undefined}
          >
            {micMuted
              ? <MicrophoneSlash size={14} weight="regular" />
              : <Microphone size={14} weight="regular" />}
          </button>
        )}
        <button
          onClick={onOpenDeviceSettings}
          className="icon-btn"
          title="デバイス設定">
          <SlidersHorizontal size={14} weight="regular" />
        </button>

        <div className="rec-controls" data-state={recording ? "rec" : "idle"}>
          {recording && (
            <button
              onClick={togglePaused}
              className="rec-pause"
              title={paused ? "録音を再開" : "録音を一時停止"}
              aria-label={paused ? "録音を再開" : "録音を一時停止"}>
              {paused ? <Play size={14} weight="regular" /> : <Pause size={14} weight="regular" />}
            </button>
          )}

          <button
            onClick={handleMainClick}
            disabled={recordingStopping}
            className="rec-main"
            aria-label={recordingStarting ? "録音開始をキャンセル" : recordingStopping ? "録音停止中" : recording ? "録音を停止" : "録音開始"}
            title={recordingStarting ? "録音開始をキャンセル" : recordingStopping ? "録音停止中" : recording ? "録音停止" : "録音開始"}>
            <span className="rec-main-icon rec-main-icon-mic">
              {recordingStarting ? <Stop size={14} weight="fill" /> : <Microphone size={18} weight="fill" />}
            </span>
            <span className="rec-main-icon rec-main-icon-stop">
              <Stop size={14} weight="fill" />
            </span>
          </button>
        </div>
      </div>

      {error && (
        <span className="absolute right-4 -bottom-5 text-[11px] text-(--danger)">{error}</span>
      )}
    </div>
  );
}
