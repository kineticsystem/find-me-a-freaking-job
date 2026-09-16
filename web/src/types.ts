export type Status = 'new' | 'shortlisted' | 'applied' | 'dismissed' | 'archived'
export type RemoteType = 'remote' | 'hybrid' | 'onsite' | 'unknown'
export type Verdict = 'strong' | 'maybe' | 'reject'
export type Sort = 'score' | 'newest' | 'company'

export const STATUSES: Status[] = ['new', 'shortlisted', 'applied', 'dismissed', 'archived']
// Offered as chips when dismissing a job. Free text is also accepted; these
// are just the objections that come up most often.
export const DISMISS_REASONS = [
  'salary too low',
  'on-site',
  'wrong stack',
  'agency / consultancy',
  'too junior',
  'too senior',
  'not eligible',
  'not interested in the company',
  'not interested in the domain',
] as const

export const REMOTE_TYPES: RemoteType[] = ['remote', 'hybrid', 'onsite', 'unknown']

export interface Job {
  id: number
  fingerprint: string
  source_id: string
  company: string
  title: string
  location: string | null
  remote_type: RemoteType | null
  url: string | null
  apply_url: string | null
  description: string | null
  salary_raw: string | null
  posted_at: string | null
  tags: string[]
  first_seen: string
  last_seen: string
  seen_count: number
  status: Status
  notes: string | null
  reason: string | null
  // from the best current evaluation; null until the model has looked at it
  score: number | null
  score_stale: number          // 1 = from before the last CV/preferences change; re-scored on the next scan
  verdict: Verdict | null
  summary: string | null
  eligibility: string | null
  eligible: number | null
  salary: string | null
  tech_stack: string[]
  concerns: string[]
  rationale: string | null
}

export interface JobPage {
  count: number
  total: number
  offset: number
  limit: number
  jobs: Job[]
}

export interface Facets {
  status: Record<string, number>
  source: Record<string, number>
  remote: Record<string, number>
}

export interface Progress {
  active: boolean
  run_id?: number
  stage?: string
  message?: string
  current?: number
  total?: number
  eta_seconds?: number | null
  elapsed_seconds?: number
  fetched?: number
  sources?: number
  stopping?: boolean
}

export interface User {
  id: number
  name: string
  email: string | null
  is_admin: boolean
  can_login: boolean
  created_at: string
  tokens?: number
}

export interface LoginResult { token: string; expires_at: string; user: User }

export interface Health {
  ok: boolean
  needs_setup: boolean
  progress: Progress
  stale_scores: number | null
  /** Per-user parts: null when the request carried no valid token. */
  setup: { cv: boolean; notes: boolean; preferences: boolean; ready: boolean } | null
  running: boolean
  next_run: string | null
  interval_minutes: number
  stats: Record<string, number> | null
}

export interface JobQuery {
  q: string
  status: Status | ''
  remote: RemoteType | ''
  source: string
  minScore: number
  sort: Sort
  hidden: boolean          // show only archived and dismissed
}

export const DEFAULT_QUERY: JobQuery = {
  q: '',
  status: '',
  remote: '',
  source: '',
  minScore: 0,
  sort: 'score',
  hidden: false,
}

export interface Settings {
  interval_minutes: number
  run_on_start: boolean
  next_run: string | null
  running: boolean
}

export interface Profile {
  cv: { name: string; bytes: number; modified: string } | null
  cv_files: string[]
  cv_chars: number
  notes: string
  digest: { headline: string; summary: string } | null
  digest_current: boolean
}

export interface Source {
  id: string
  type: string
  origin: 'config' | 'discovered' | 'user' | 'keyword'
  enabled: number          // the registry switch (auto-off after repeated failures)
  following: number        // your own switch
  config: Record<string, unknown>
  added_at: string
  last_run_at: string | null
  last_ok_at: string | null
  last_error: string | null
  jobs_found: number
  jobs_stored: number
  fail_count: number
  deletable: boolean
}

export interface LocationRule {
  country?: string | null
  region?: string | null
  remote: 'required' | 'any' | 'no'
  priority?: number
  note?: string | null
}

export interface Preferences {
  based_in: string
  citizenship: string[]
  work_authorization_notes: string
  titles: string[]
  seniority: string[]
  location_rules: LocationRule[]
  market_priority: string[]
  languages: string[]
  must_have: string[]
  nice_to_have: string[]
  dealbreakers: string[]
  min_salary: { amount: number; currency: string; period: 'year' | 'month' | 'day' | 'hour' } | null
}

export const SENIORITIES = ['junior', 'mid', 'senior', 'staff', 'principal', 'lead'] as const
export const CURRENCIES = ['EUR', 'USD', 'GBP', 'PLN', 'CHF'] as const
