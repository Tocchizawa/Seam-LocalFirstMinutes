import { getVersion } from "@tauri-apps/api/app";
import { check, type DownloadEvent, type Update } from "@tauri-apps/plugin-updater";
import { relaunch } from "@tauri-apps/plugin-process";

export interface AppUpdateInfo {
  currentVersion: string;
  version: string;
  date?: string;
  notes?: string;
  update: Update;
}

export interface AppUpdateProgress {
  phase: "downloading" | "installing" | "restarting";
  downloadedBytes?: number;
  totalBytes?: number;
  percent?: number;
  message: string;
}

export async function getCurrentAppVersion(): Promise<string> {
  return getVersion();
}

export async function checkForAppUpdate(options: { timeoutMs?: number } = {}): Promise<AppUpdateInfo | null> {
  const update = await check({ timeout: options.timeoutMs ?? 15_000 });
  if (!update) return null;
  return {
    currentVersion: update.currentVersion,
    version: update.version,
    date: update.date,
    notes: update.body,
    update,
  };
}

export async function closeAppUpdate(update: AppUpdateInfo | null | undefined): Promise<void> {
  try {
    await update?.update.close();
  } catch {
    // Tauri resource cleanup only. Failure here should not block UI flow.
  }
}

export async function installAndRelaunchAppUpdate(
  info: AppUpdateInfo,
  onProgress?: (progress: AppUpdateProgress) => void,
  options: {
    shouldContinue?: () => boolean | Promise<boolean>;
    abortMessage?: string;
  } = {},
): Promise<void> {
  let downloadedBytes = 0;
  let totalBytes: number | undefined;

  const ensureCanContinue = async () => {
    if (options.shouldContinue && !(await options.shouldContinue())) {
      throw new Error(options.abortMessage ?? "アップデートを中断しました");
    }
  };

  await ensureCanContinue();
  await info.update.download((event: DownloadEvent) => {
    if (event.event === "Started") {
      downloadedBytes = 0;
      totalBytes = event.data.contentLength;
      onProgress?.({
        phase: "downloading",
        downloadedBytes,
        totalBytes,
        percent: undefined,
        message: "アップデートをダウンロード中...",
      });
      return;
    }

    if (event.event === "Progress") {
      downloadedBytes += event.data.chunkLength;
      const percent = totalBytes && totalBytes > 0
        ? Math.min(100, Math.round((downloadedBytes / totalBytes) * 100))
        : undefined;
      onProgress?.({
        phase: "downloading",
        downloadedBytes,
        totalBytes,
        percent,
        message: percent === undefined
          ? "アップデートをダウンロード中..."
          : `アップデートをダウンロード中... ${percent}%`,
      });
      return;
    }

    if (event.event === "Finished") return;

    onProgress?.({
      phase: "installing",
      downloadedBytes,
      totalBytes,
      percent: 100,
      message: "アップデートを適用中...",
    });
  }, { timeout: 10 * 60 * 1000 });

  await ensureCanContinue();
  onProgress?.({
    phase: "installing",
    downloadedBytes,
    totalBytes,
    percent: 100,
    message: "アップデートを適用中...",
  });
  await info.update.install();

  await ensureCanContinue();
  onProgress?.({
    phase: "restarting",
    downloadedBytes,
    totalBytes,
    percent: 100,
    message: "再起動してアップデートを完了します...",
  });
  await relaunch();
}

export function updateErrorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === "string" && error.trim()) return error;
  return "アップデート処理に失敗しました";
}
