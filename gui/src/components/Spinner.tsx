interface Props {
  size?: number;
  color?: string;
  className?: string;
}

export function Spinner({ size = 16, color, className = "" }: Props) {
  const borderWidth = Math.max(1.5, size / 14);
  return (
    <div
      className={`rounded-full anim-spin ${className}`}
      style={{
        width: size,
        height: size,
        border: `${borderWidth}px solid var(--t4)`,
        borderTopColor: color ?? "var(--t2)",
      }}
    />
  );
}
