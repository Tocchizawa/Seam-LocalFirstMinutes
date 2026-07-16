/**
 * テーマ管理。
 * - "dark" / "light" / "system" の3モード
 * - localStorage に永続化
 * - "system" の場合は prefers-color-scheme に追従
 * - <html data-theme="dark|light"> として DOM に適用
 */

export type ThemeMode = "dark" | "light" | "system";

const STORAGE_KEY = "seam.theme";

function readStored(): ThemeMode {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === "dark" || v === "light" || v === "system") return v;
  } catch {
    // localStorage 不可
  }
  return "system";
}

function systemPrefersDark(): boolean {
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? true;
}

function resolveActual(mode: ThemeMode): "dark" | "light" {
  if (mode === "system") return systemPrefersDark() ? "dark" : "light";
  return mode;
}

function apply(mode: ThemeMode): void {
  const actual = resolveActual(mode);
  document.documentElement.setAttribute("data-theme", actual);
}

export function getThemeMode(): ThemeMode {
  return readStored();
}

export function setThemeMode(mode: ThemeMode): void {
  try {
    localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    // ignore
  }
  apply(mode);
}

export function initTheme(): () => void {
  const current = readStored();
  apply(current);

  // system 追従の場合は OS 設定変更を監視
  const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
  const onChange = () => {
    if (readStored() === "system") apply("system");
  };
  mq?.addEventListener?.("change", onChange);
  return () => mq?.removeEventListener?.("change", onChange);
}

export function getResolvedTheme(): "dark" | "light" {
  return resolveActual(readStored());
}
