export type Unit = 'minutes' | 'hours' | 'days'
export const UNIT_MINUTES: Record<Unit, number> = { minutes: 1, hours: 60, days: 1440 }

// Show an interval in the largest unit that divides it exactly.
export function split(minutes: number): { n: number; unit: Unit } {
  if (minutes % 1440 === 0) return { n: minutes / 1440, unit: 'days' }
  if (minutes % 60 === 0) return { n: minutes / 60, unit: 'hours' }
  return { n: minutes, unit: 'minutes' }
}

export function describe(minutes: number): string {
  const { n, unit } = split(minutes)
  return `${n} ${n === 1 ? unit.slice(0, -1) : unit}`
}
