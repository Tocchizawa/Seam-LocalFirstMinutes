/**
 * 円形プログレス。中央に % を表示。
 * stroke-dashoffset でぐるっと描画するシンプル SVG。
 */
interface Props {
  /** 0-100 */
  percent: number;
  size?: number;
  stroke?: number;
  color?: string;
  trackColor?: string;
}

export function CircleProgress({
  percent,
  size = 96,
  stroke = 5,
  color = "var(--accent)",
  trackColor = "var(--surface-2)",
}: Props) {
  const clamped = Math.max(0, Math.min(100, percent));
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const offset = c - (clamped / 100) * c;
  const cx = size / 2;
  const cy = size / 2;
  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      style={{ display: "block" }}
    >
      <circle
        cx={cx}
        cy={cy}
        r={r}
        fill="none"
        stroke={trackColor}
        strokeWidth={stroke}
      />
      <circle
        cx={cx}
        cy={cy}
        r={r}
        fill="none"
        stroke={color}
        strokeWidth={stroke}
        strokeDasharray={c}
        strokeDashoffset={offset}
        strokeLinecap="round"
        transform={`rotate(-90 ${cx} ${cy})`}
        style={{ transition: "stroke-dashoffset 280ms cubic-bezier(0.16, 1, 0.3, 1)" }}
      />
      <text
        x="50%"
        y="50%"
        textAnchor="middle"
        dominantBaseline="central"
        fontSize={size * 0.22}
        fontWeight={600}
        fill="var(--t1)"
        style={{ fontVariantNumeric: "tabular-nums" }}
      >
        {Math.round(clamped)}%
      </text>
    </svg>
  );
}
