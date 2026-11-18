const LEVEL_BARS = 24;

export function Waveform({ level }: { level: number }) {
  const lit = Math.ceil(Math.min(1, Math.max(0, level)) * LEVEL_BARS);

  return (
    <span className="input-level" aria-hidden="true">
      {Array.from({ length: LEVEL_BARS }, (_, index) => (
        <span key={index} data-lit={index < lit || undefined} />
      ))}
    </span>
  );
}
