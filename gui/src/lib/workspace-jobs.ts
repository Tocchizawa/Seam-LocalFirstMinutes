import { useCallback, useEffect, useRef, useState } from "react";
import {
  getActiveSummarizes, getPipelines, getRecoveryStatus,
  type ActiveSummarize, type PipelineStatus, type RecoveryStatus,
} from "./api";

const JOB_EVENTS = [
  "__open", "pipeline_progress", "pipeline_done", "pipeline_error", "recording_stopped",
  "summary_stage", "summary_done", "summary_failed", "summary_cancelled", "summary_skipped",
  "dictionary_update_started", "dictionary_update_finished",
  "session_recovery_started", "session_recovery_progress", "session_recovery_finished",
].map((name) => `recording-ws:${name}`);

export function useWorkspaceJobs(enabled: boolean) {
  const [pipelines, setPipelines] = useState<PipelineStatus[]>([]);
  const [summaries, setSummaries] = useState<ActiveSummarize[]>([]);
  const [recovery, setRecovery] = useState<RecoveryStatus | null>(null);
  const request = useRef<() => void>(() => {});
  const refresh = useCallback(() => request.current(), []);

  useEffect(() => {
    if (!enabled) return;
    let disposed = false;
    let loading = false;
    let pending = false;
    let timer = 0;

    const load = async () => {
      if (disposed || loading) return;
      loading = true;
      pending = false;
      const [nextPipelines, nextSummaries, nextRecovery] = await Promise.allSettled([
        getPipelines(), getActiveSummarizes(), getRecoveryStatus(),
      ]);
      loading = false;
      if (disposed) return;
      if (nextPipelines.status === "fulfilled") setPipelines(nextPipelines.value);
      if (nextSummaries.status === "fulfilled") setSummaries(nextSummaries.value);
      if (nextRecovery.status === "fulfilled") setRecovery(nextRecovery.value);
      if (pending) schedule();
    };

    const schedule = () => {
      pending = true;
      if (loading || timer) return;
      timer = window.setTimeout(() => {
        timer = 0;
        void load();
      }, 80);
    };

    request.current = schedule;
    JOB_EVENTS.forEach((name) => window.addEventListener(name, schedule));
    const poll = window.setInterval(schedule, 5000);
    void load();
    return () => {
      disposed = true;
      request.current = () => {};
      window.clearTimeout(timer);
      window.clearInterval(poll);
      JOB_EVENTS.forEach((name) => window.removeEventListener(name, schedule));
    };
  }, [enabled]);

  return { pipelines, summaries, recovery, refresh };
}
