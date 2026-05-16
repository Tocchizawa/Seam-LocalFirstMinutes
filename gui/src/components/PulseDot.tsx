/**
 * 「稼働中」をさりげなく示す小さな SVG インジケータ。
 * 内側の小さなドットがソフトに呼吸 + 外側のリングがゆっくり広がって消える(ソナー風)。
 * Spinner より目立たない代わりに、ずっと回り続けるストレスが無い。
 */
interface Props {
  size?: number;
  color?: string;
  /** アニメーション周期 (sec) */
  duration?: number;
}

export function PulseDot({
  size = 9,
  color = "var(--accent)",
  duration = 1.8,
}: Props) {
  const center = size / 2;
  const r = size * 0.22;
  const ringMax = size * 0.46;
  const dur = `${duration}s`;
  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      aria-hidden="true"
      style={{ flexShrink: 0, overflow: "visible" }}
    >
      {/* 拡張するリング (ソナー) */}
      <circle cx={center} cy={center} r={r} fill="none" stroke={color} strokeWidth="0.7">
        <animate
          attributeName="r"
          values={`${r};${ringMax}`}
          dur={dur}
          repeatCount="indefinite"
        />
        <animate
          attributeName="opacity"
          values="0.6;0"
          dur={dur}
          repeatCount="indefinite"
        />
      </circle>
      {/* 中心のドットを薄く呼吸させる */}
      <circle cx={center} cy={center} r={r} fill={color}>
        <animate
          attributeName="opacity"
          values="0.55;1;0.55"
          dur={dur}
          repeatCount="indefinite"
        />
      </circle>
    </svg>
  );
}
