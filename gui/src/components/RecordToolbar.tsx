import { useCallback, useMemo, useState } from "react";
import {
  Microphone, MicrophoneSlash, Stop, Pause, Play, SlidersHorizontal,
} from "@phosphor-icons/react";
import { Waveform } from "./Waveform";
import {
  startRecording, stopRecording, updateSettings, setMicMuted,
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
    recording, setRecording, paused, togglePaused,
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
    setError("");
    try {
      const started = await startRecording(project.id, micDevice, captureSystem);
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
      setError(e.message || "録音の開始に失敗しました");
    }
  };

  const handleStop = useCallback(async () => {
    try { await stopRecording(); } catch {}
    setRecording(false);
    setActiveSessionId(null);
    setError("");
    onStopped();
  }, [setRecording, setActiveSessionId, onStopped]);

  const handleMainClick = recording ? handleStop : handleStart;

  const display = useMemo(() => fmt(elapsedSec), [elapsedSec]);

  return (
    <div className="toolbar">
      {/* Left: timer + waveform */}
      <div className="flex items-center gap-3 flex-1 min-w-0">
        <div className="flex flex-col items-start shrink-0 min-w-[72px]">
          <span className={`num text-[22px] leading-none ${recording ? "text-(--t1)" : "text-(--t3)"}`}>
            <span>{display}</span>
          </span>
          <span className="flex items-center gap-1.5 mt-1 h-[14px]">
            {recording ? (
              <>
                <span
                  className={`status-dot ${paused ? "" : "status-dot--pulse"}`}
                  style={{ background: paused ? "var(--t3)" : "var(--accent)" }}
                />
                <span className="text-[10px] text-(--t3)">{paused ? "Paused" : "Speaking"}</span>
              </>
            ) : (
              <span className="text-[10px] text-(--t4)">Idle</span>
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
          <button
            onClick={togglePaused}
            className="rec-pause"
            title={paused ? "再開" : "一時停止"}
            tabIndex={recording ? 0 : -1}>
            {paused ? <Play size={13} weight="fill" /> : <Pause size={13} weight="fill" />}
          </button>

          <button
            onClick={handleMainClick}
            className="rec-main"
            title={recording ? "停止" : "録音開始"}>
            <span className="rec-main-icon rec-main-icon-mic">
              <Microphone size={18} weight="fill" />
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
