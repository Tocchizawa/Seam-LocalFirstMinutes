/* IME (日本語変換) で Enter キーを誤って確定アクションとして処理しないためのヘルパ。
   3 つの条件を OR で見て弾く:
     1. e.nativeEvent.isComposing — 標準の composition 中フラグ
     2. e.nativeEvent.keyCode === 229 — IME magic code (古いブラウザ・WebKit 互換)
     3. つい先ほど compositionend が走ったか — WebKit が
        Enter で IME 確定した瞬間に isComposing=false で keydown を投げてくるケース対策
*/
import { useCallback, useRef } from "react";

export interface ImeSafeHandlers {
  onCompositionStart: () => void;
  onCompositionEnd: () => void;
}

export interface UseImeSafeEnterReturn {
  /** input / form に展開して渡す。composition の開始/終了を tracking する。 */
  imeHandlers: ImeSafeHandlers;
  /** Enter 押下時に「IME 由来か」を判定する。true なら無視すべき。 */
  isImeEnter: (e: React.KeyboardEvent) => boolean;
}

export function useImeSafeEnter(): UseImeSafeEnterReturn {
  const composingRef = useRef(false);
  const recentlyEndedRef = useRef(false);
  const recentTimerRef = useRef<number | null>(null);

  const onCompositionStart = useCallback(() => {
    composingRef.current = true;
  }, []);

  const onCompositionEnd = useCallback(() => {
    composingRef.current = false;
    recentlyEndedRef.current = true;
    if (recentTimerRef.current !== null) {
      window.clearTimeout(recentTimerRef.current);
    }
    // 50ms はほぼすべての IME 確定後の余韻 keydown をカバーする経験値
    recentTimerRef.current = window.setTimeout(() => {
      recentlyEndedRef.current = false;
      recentTimerRef.current = null;
    }, 50);
  }, []);

  const isImeEnter = useCallback((e: React.KeyboardEvent) => {
    const ne = e.nativeEvent as KeyboardEvent;
    return (
      composingRef.current
      || recentlyEndedRef.current
      || ne.isComposing
      || ne.keyCode === 229
    );
  }, []);

  return {
    imeHandlers: { onCompositionStart, onCompositionEnd },
    isImeEnter,
  };
}
