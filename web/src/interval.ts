export type Unit = 'minutes' | 'hours' | 'days'
export const UNIT_MINUTES: Record<Unit, number> = { minutes: 1, hours: 60, days: 1440 }

// Show an interval in the largest unit that divides it exactly.
export function split(minutes: number): { n: number; unit: Unit } {
  if (minutes % 1440 === 0) return { n: minutes / 1440, unit: 'days' }
  if (minutes % 60 === 0) return { n: minutes / 60, unit: 'hours' }
  return { n: minutes, unit: 'minutes' }
}

/** A duration in seconds as people say it: "48 s", "2 min", "1 h 52 min". */
export function duration(seconds: number): string {
  const s = Math.round(seconds)
  if (s < 60) return `${s} s`
  const m = Math.round(s / 60)
  if (m < 60) return `${m} min`
  const h = Math.floor(m / 60); const r = m % 60
  return r ? `${h} h ${r} min` : `${h} h`
}

export function describe(minutes: number): string {
  const { n, unit } = split(minutes)
  return `${n} ${n === 1 ? unit.slice(0, -1) : unit}`
}
