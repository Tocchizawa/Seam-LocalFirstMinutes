/**
 * グローバル toast ストア。
 * どこからでも showToast({...}) を呼ぶと右下にトーストが出る。
 * subscribeToasts() で UI 側が購読する。
 */

export type ToastKind = "ok" | "err" | "info";

export interface ToastItem {
  id: string;
  kind: ToastKind;
  text: string;
  ttl: number; // ms 表示時間
  /** クリック時のアクション。あればトースト本体がボタン化される。 */
  onClick?: () => void;
  /** クリック後にトーストを自動 dismiss するか。デフォルト true。 */
  dismissOnClick?: boolean;
  /** ホバー時の補足ラベル (例: ファイルパス)。 */
  hoverHint?: string;
}

type Listener = (items: ToastItem[]) => void;
const listeners = new Set<Listener>();
let items: ToastItem[] = [];
const timers = new Map<string, number>();

function emit(): void {
  listeners.forEach((l) => l(items));
}

export function showToast(input: {
  kind: ToastKind;
  text: string;
  ttl?: number;
  onClick?: () => void;
  dismissOnClick?: boolean;
  hoverHint?: string;
}): string {
  const id = Math.random().toString(36).slice(2, 9);
  // onClick 付きトーストは少し長めに表示 (ユーザーが押しに行く時間を確保)
  const defaultTtl = input.onClick ? 5000 : 2600;
  const item: ToastItem = {
    id,
    kind: input.kind,
    text: input.text,
    ttl: input.ttl ?? defaultTtl,
    onClick: input.onClick,
    dismissOnClick: input.dismissOnClick,
    hoverHint: input.hoverHint,
  };
  items = [item, ...items].slice(0, 5);
  emit();

  const t = window.setTimeout(() => dismissToast(id), item.ttl);
  timers.set(id, t);
  return id;
}

export function dismissToast(id: string): void {
  const t = timers.get(id);
  if (t !== undefined) {
    clearTimeout(t);
    timers.delete(id);
  }
  if (items.find((x) => x.id === id)) {
    items = items.filter((x) => x.id !== id);
    emit();
  }
}

export function subscribeToasts(fn: Listener): () => void {
  listeners.add(fn);
  fn(items);
  return () => { listeners.delete(fn); };
}
