import { useState } from 'react'

interface Props {
  id?: string
  values: string[]
  onChange: (values: string[]) => void
  placeholder?: string
}

/** A list of short strings: type, Enter or comma to add, × to remove. */
export function ChipInput({ id, values, onChange, placeholder }: Props) {
  const [draft, setDraft] = useState('')
  const commit = () => {
    const parts = draft.split(',').map((s) => s.trim()).filter(Boolean)
    if (parts.length) onChange([...values, ...parts.filter((p) => !values.includes(p))])
    setDraft('')
  }
  return (
    <div className="chip-input">
      {values.map((v) => (
        <span className="chip chip-value" key={v}>
          {v}
          <button type="button" aria-label={`remove ${v}`} onClick={() => onChange(values.filter((x) => x !== v))}>×</button>
        </span>
      ))}
      <input
        id={id} value={draft} placeholder={values.length ? '' : placeholder}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); commit() }
          if (e.key === 'Backspace' && !draft && values.length) onChange(values.slice(0, -1))
        }}
        onBlur={commit}
      />
    </div>
  )
}
