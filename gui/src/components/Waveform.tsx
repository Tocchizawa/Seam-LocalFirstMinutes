import { useEffect, useRef } from "react";

interface Props {
  /** 0..1 の音声レベル */
  level: number;
  /** true: 時間ベースで生体感を出す。false: 完全静止(rAF 停止) */
  alive?: boolean;
  bars?: number;
  height?: number;
  opacity?: number;
  className?: string;
}

/**
 * audio-reactive Waveform。
 * alive=true: requestAnimationFrame でアニメーション。
 * alive=false: 1 度だけ描画して停止(CPU を食わない)。
 */
export function Waveform({
  level,
  alive = true,
  bars,
  height = 120,
  opacity = 1,
  className = "",
}: Props) {
  const ref = useRef<HTMLCanvasElement | null>(null);
  const levelRef = useRef(level);
  const smoothLevelRef = useRef(level);
  levelRef.current = level;

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;

    // バー個別の位相 / 周波数 / 振幅
    const phases: number[] = Array.from({ length: 1024 }, () => Math.random() * Math.PI * 2);
    const freqs: number[] = Array.from({ length: 1024 }, () => 0.6 + Math.random() * 0.8);
    const amps: number[] = Array.from({ length: 1024 }, () => 0.7 + Math.random() * 0.6);

    let raf = 0;
    let stopped = false;

    const drawFrame = (t: number) => {
      const w = canvas.clientWidth;
      const h = canvas.clientHeight;
      ctx.clearRect(0, 0, w, h);

      const barW = 2;
      const gap = 2;
      const stride = barW + gap;
      const count = bars ?? Math.floor(w / stride);
      const startX = (w - count * stride) / 2;
      const cx = count / 2;

      const target = levelRef.current;
      smoothLevelRef.current += (target - smoothLevelRef.current) * 0.18;
      const lvl = smoothLevelRef.current;

      const aliveBase = alive ? 0.08 : 0;
      const levelGain = Math.min(lvl, 1) * 0.85;
      const baseAmp = aliveBase + levelGain;

      const accent = getCSSVar("--t1") || "#f5f5f7";
      const dim = getCSSVar("--t4") || "rgba(255,255,255,0.2)";

      for (let i = 0; i < count; i++) {
        const dist = Math.abs(i - cx) / cx;
        const env = Math.pow(1 - dist, 1.4);

        const phase = phases[i % phases.length];
        const freq = freqs[i % freqs.length];
        const ampMod = amps[i % amps.length];

        const aliveWobble = alive
          ? Math.sin(t * 0.0035 * freq + phase) * 0.18
            + Math.sin(t * 0.0014 * freq + phase * 1.7) * 0.12
            + Math.cos(t * 0.0008 * freq + phase * 0.3) * 0.08
          : 0;

        const speechWobble = (alive && lvl > 0.02)
          ? Math.sin(t * 0.012 + i * 0.4 + phase) * lvl * 0.55
            + Math.sin(t * 0.024 + i * 0.7 + phase * 2) * lvl * 0.35
          : 0;

        const amp = Math.max(
          0.025,
          baseAmp * env * ampMod + aliveWobble * env * 0.7 + speechWobble * env
        );
        const barH = Math.max(2, amp * h);
        const x = startX + i * stride;
        const y = (h - barH) / 2;

        ctx.fillStyle = dist < 0.6 ? accent : dim;
        ctx.globalAlpha = opacity * (0.35 + (1 - dist) * 0.65);
        ctx.fillRect(x, y, barW, barH);
      }

      if (!stopped && alive) {
        raf = requestAnimationFrame(drawFrame);
      }
    };

    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      canvas.width = Math.floor(rect.width * dpr);
      canvas.height = Math.floor(rect.height * dpr);
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.scale(dpr, dpr);
      // alive=false の場合、サイズ変更後に再描画
      if (!alive) drawFrame(performance.now());
    };

    let resizeObs: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined") {
      resizeObs = new ResizeObserver(resize);
      resizeObs.observe(canvas);
    }
    resize();

    if (alive) {
      raf = requestAnimationFrame(drawFrame);
    } else {
      drawFrame(performance.now());
    }

    return () => {
      stopped = true;
      cancelAnimationFrame(raf);
      resizeObs?.disconnect();
    };
  }, [alive, bars, opacity]);

  return (
    <canvas
      ref={ref}
      className={className}
      style={{ width: "100%", height, display: "block" }}
    />
  );
}

function getCSSVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
