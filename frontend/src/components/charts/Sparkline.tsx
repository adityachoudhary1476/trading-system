import { useMemo } from "react";

/** Tiny inline SVG sparkline. Purely presentational; data is mock/deterministic. */
export function Sparkline({
  data,
  width = 56,
  height = 20,
  positive,
}: {
  data: number[];
  width?: number;
  height?: number;
  positive?: boolean;
}) {
  const path = useMemo(() => {
    if (!data.length) return "";
    const min = Math.min(...data);
    const max = Math.max(...data);
    const span = max - min || 1;
    const step = width / (data.length - 1 || 1);
    return data
      .map((v, i) => {
        const x = i * step;
        const y = height - ((v - min) / span) * (height - 2) - 1;
        return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
  }, [data, width, height]);

  const color = positive ? "var(--positive)" : positive === false ? "var(--negative)" : "var(--neutral)";
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true" style={{ display: "block" }}>
      <path d={path} fill="none" stroke={color} strokeWidth={1.4} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}
