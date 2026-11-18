import {
  isPermissionGranted,
  requestPermission,
  sendNotification,
} from "@tauri-apps/plugin-notification";

let cachedGranted: boolean | null = null;

/** macOS の通知許可を確認 (未確認なら要求) して結果をキャッシュする。 */
export async function ensureNotificationPermission(): Promise<boolean> {
  if (cachedGranted !== null) return cachedGranted;
  try {
    let granted = await isPermissionGranted();
    if (!granted) {
      granted = (await requestPermission()) === "granted";
    }
    cachedGranted = granted;
    return granted;
  } catch {
    cachedGranted = false;
    return false;
  }
}

/**
 * macOS のネイティブ通知を送る。
 * 許可が得られなかった / 失敗した場合は false を返す (呼び出し側で toast 等にフォールバック)。
 */
export async function notifyNative(title: string, body: string): Promise<boolean> {
  const granted = await ensureNotificationPermission();
  if (!granted) return false;
  try {
    sendNotification({ title, body });
    return true;
  } catch {
    return false;
  }
}
