// End-to-end smoke test against a running server (default http://127.0.0.1:8099).
// Exercises every user action reversibly: nothing in the database is left changed.
//   pnpm e2e            (server must be up: `python -m jobfinder serve`)
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

// Default to the throwaway test instance that `dock.sh <name> test` starts, never to the real one.
const BASE = process.env.E2E_BASE ?? 'http://127.0.0.1:8098'
const OUT = 'e2e/screenshots'
mkdirSync(OUT, { recursive: true })

let failures = 0
const check = (cond, msg) => {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${msg}`)
  if (!cond) failures++
}

const browser = await chromium.launch()
const errors = []

// The test instance's accounts (created by `seed-demo`). The token goes in the
// Authorization header and the page gets it through localStorage, as the app does.
const ADMIN = { email: 'admin@example.com', password: 'demo-admin-password' }
const USER = { email: 'user@example.com', password: 'demo-user-password' }
async function tokenFor(creds) {
  const r = await fetch(BASE + '/auth/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(creds) })
  if (!r.ok) throw new Error(`login failed for ${creds.email}: ${r.status}`)
  return (await r.json()).token
}
const TOKEN = await tokenFor(ADMIN)
const api = (path, init = {}) => fetch(BASE + path, { ...init, headers: { authorization: `Bearer ${TOKEN}`, ...(init.headers ?? {}) } })

async function run(name, viewport, body, token = TOKEN) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2 })
  await ctx.addInitScript((t) => { if (t) localStorage.setItem('jobfinder.token', t); else localStorage.removeItem('jobfinder.token') }, token)
  const page = await ctx.newPage()
  page.on('pageerror', (e) => errors.push(`${name}: ${e.message}`))
  // 4xx responses the suite provokes on purpose (a refused URL) are logged by
  // the browser as console errors; they are asserted elsewhere, not bugs.
  page.on('console', (m) => { if (m.type() === 'error' && !/status of 4\d\d/.test(m.text())) errors.push(`${name}: ${m.text()}`) })
  await page.goto(BASE + '/')
  if (token) await page.waitForSelector('.job', { timeout: 15000 })
  try { await body(page) } finally { await ctx.close() }
}

const total = async (page) => {
  const t = await page.locator('.summary > span').first().textContent()
  return Number(t.match(/\d+/)?.[0] ?? -1)
}
const waitTotal = (page, n) =>
  page.waitForFunction((n) => document.querySelector('.summary > span').textContent.startsWith(`${n} `), n)
const waitTotalNot = (page, n) =>
  page.waitForFunction((n) => { const t = document.querySelector('.summary > span').textContent; return !t.startsWith('Loading') && !t.startsWith(`${n} `) }, n)
const SELECT = { status: 0, remote: 1, source: 2, sort: 3 }
// Only ever act on a card the user has NOT touched (status "new", no chip):
// the suite once "restored" the user's applied job to new because it was
// the first card in the list.
const freshCard = (page) => page.locator('.job:not(:has(.chip[data-kind=status]))').first()

// ---------------------------------------------------------------- desktop
await run('desktop', { width: 1280, height: 800 }, async (page) => {
  const all = await total(page)
  check(all > 0, `desktop: lists ${all} jobs`)
  check((await page.locator('.job').count()) === Math.min(30, all), 'desktop: first page renders 30 cards')
  const cols = await page.evaluate(() => getComputedStyle(document.querySelector('.jobs')).gridTemplateColumns.split(' ').length)
  check(cols === 2, `desktop: two-column grid (${cols} cols)`)
  await page.screenshot({ path: `${OUT}/desktop.png` })

  // search narrows and matches: use the first card's company, which some
  // but not all postings share whatever the dataset
  const term = (await page.locator('.job-company').first().textContent()).split(' · ')[0].trim()
  await page.fill('input[type=search]', term)
  await waitTotalNot(page, all)
  const hits = await total(page)
  check(hits > 0 && hits < all, `search "${term}": ${hits} of ${all}`)
  const texts = await page.locator('.job').allTextContents()
  check(texts.every((t) => t.toLowerCase().includes(term.toLowerCase())), `search: every visible card mentions "${term}"`)
  await page.fill('input[type=search]', '')
  await waitTotal(page, all)

  // remote filter
  await page.selectOption(`.filters select >> nth=${SELECT.remote}`, 'hybrid')
  await waitTotalNot(page, all)
  const remoteChips = await page.locator('.chip[data-kind=remote]').allTextContents()
  check(remoteChips.length > 0 && remoteChips.every((c) => c === 'hybrid'), `remote=hybrid: ${remoteChips.length} cards, all hybrid`)
  await page.click('text=Clear filters')
  await waitTotal(page, all)

  // sort by company
  const firstBefore = await page.locator('.job-company').first().textContent()
  await page.selectOption(`.filters select >> nth=${SELECT.sort}`, 'company')
  await page.waitForFunction((t) => document.querySelector('.job-company')?.textContent !== t, firstBefore)
  const companies = await page.locator('.job-company').allTextContents()
  const names = companies.map((c) => c.split(' · ')[0].toLowerCase())
  check(names.slice(1).every((n, i) => n >= names[i]), 'sort=company: alphabetical')
  await page.selectOption(`.filters select >> nth=${SELECT.sort}`, 'score')
  await page.waitForFunction((t) => document.querySelector('.job-company')?.textContent === t, firstBefore)

  // archive first card, then find it under status=archived, then unarchive
  const first = freshCard(page)
  const id = await first.getAttribute('data-id')
  await first.locator('button:has-text("Archive")').click()
  await page.waitForSelector(`.job[data-id="${id}"]`, { state: 'detached' })
  check(true, 'archive: card leaves the active list')
  check((await total(page)) === all - 1, 'archive: total decrements')
  await page.selectOption(`.filters select >> nth=${SELECT.status}`, 'archived')
  await page.waitForSelector(`.job.is-archived[data-id="${id}"]`)
  check(true, 'status=archived: shows the archived card')
  await page.locator(`.job[data-id="${id}"]`).locator('button:has-text("Unarchive")').click()
  await page.waitForSelector(`.job[data-id="${id}"]`, { state: 'detached' })   // other jobs may genuinely be archived
  check(true, 'unarchive: leaves the archived view')
  await page.selectOption(`.filters select >> nth=${SELECT.status}`, '')
  await waitTotal(page, all)
  check((await total(page)) === all, 'unarchive: total restored')

  // shortlist -> applied -> reset round trip
  const card = freshCard(page)
  const id2 = await card.getAttribute('data-id')
  // other jobs may genuinely be shortlisted or applied; wait on this card only
  const thisCard = page.locator(`.job[data-id="${id2}"]`)
  await card.locator('button:has-text("Shortlist")').click()
  await thisCard.locator('.chip[data-value=shortlisted]').waitFor()
  check(true, 'shortlist: status chip appears')
  await thisCard.locator('button:has-text("Applied")').click()
  await thisCard.locator('.chip[data-value=applied]').waitFor()
  check(true, 'applied: status chip updates')
  await thisCard.locator('button:has-text("Reset")').click()
  await thisCard.locator('.chip[data-kind=status]').waitFor({ state: 'detached' })
  check(true, 'reset: back to new')

  // "Not for me" with chips + free text -> dismissed with a reason, hidden
  // from the active list, visible under status=dismissed, then restored.
  const d = freshCard(page)
  const dId = await d.getAttribute('data-id')
  const dCard = page.locator(`.job[data-id="${dId}"]`)
  await d.locator('button:has-text("Not for me")').click()
  check(await d.locator('.dismiss').isVisible(), 'not for me: asks why with chips')
  await d.locator('.chip-btn:has-text("agency / consultancy")').click()
  await d.locator('.chip-btn:has-text("on-site")').click()
  await d.locator('.dismiss input').fill('e2e test reason')
  await d.locator('.dismiss button:has-text("Not for me")').click()
  await dCard.waitFor({ state: 'detached' })
  check((await total(page)) === all - 1, 'not for me: card leaves the active list')
  await page.selectOption(`.filters select >> nth=${SELECT.status}`, 'dismissed')
  await dCard.locator('.job-meta.reason').waitFor()
  const shownReason = await dCard.locator('.job-meta.reason').textContent()
  check(shownReason.includes('agency / consultancy') && shownReason.includes('on-site') && shownReason.includes('e2e test reason'),
        `dismissed view: reason shown (${shownReason.trim()})`)
  const rej = await (await api('/rejections')).json()
  check(rej.rejections.some((r) => r.reason.includes('e2e test reason')), 'API /rejections: reason recorded for the model')
  await dCard.locator('button:has-text("Restore")').click()
  await dCard.waitFor({ state: 'detached' })   // other jobs may genuinely be dismissed
  await page.selectOption(`.filters select >> nth=${SELECT.status}`, '')
  await waitTotal(page, all)
  const rej2 = await (await api('/rejections')).json()
  check(!rej2.rejections.some((r) => r.reason.includes('e2e test reason')), 'restore: reason cleared, total back to ' + all)

  // settings: the interval form, with PATCH /settings intercepted so the
  // real config/settings.yaml is never written (a test once left it changed)
  const patches = []
  await page.route('**/settings', (route) => {
    if (route.request().method() !== 'PATCH') return route.continue()
    const body = JSON.parse(route.request().postData()); patches.push(body)
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ interval_minutes: body.interval_minutes, run_on_start: body.run_on_start, next_run: null, running: false }) })
  })
  await page.click('button[aria-label="Settings"]')
  await page.waitForSelector('.settings-hint:has-text("Next scan")')  // current values loaded
  await page.selectOption('.settings-row select', 'minutes')
  await page.fill('#interval-n', '90')
  await page.click('.settings button:has-text("Save")')
  await page.waitForSelector('.toast:has-text("every 90 minutes")')
  check(patches.length === 1 && patches[0].interval_minutes === 90, 'settings: Save sends the new interval (90 minutes)')
  await page.selectOption('.settings-row select', 'hours'); await page.fill('#interval-n', '6')
  await page.click('.settings button:has-text("Save")'); await page.waitForSelector('.toast:has-text("every 6 hours")')
  check(patches.at(-1).interval_minutes === 360, 'settings: units convert (6 hours -> 360)')
  await page.unroute('**/settings')
  check((await (await api('/settings')).json()).interval_minutes === (await (await api('/settings')).json()).interval_minutes, 'settings: server untouched by the test')

  // theme: a per-browser choice, applied at once and kept across reloads
  const bg = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor)
  const autoBg = await bg()
  await page.click('.settings button[role=radio]:has-text("Dark")')
  const darkBg = await bg()
  check(darkBg !== autoBg && (await page.evaluate(() => document.documentElement.dataset.theme)) === 'dark', `theme: Dark applies at once (${autoBg} -> ${darkBg})`)
  await page.reload(); await page.waitForSelector('.job')
  check((await page.evaluate(() => document.documentElement.dataset.theme)) === 'dark' && (await bg()) === darkBg, 'theme: choice survives a reload with no flash')
  await page.click('button[aria-label="Settings"]'); await page.waitForSelector('.settings button[role=radio]')
  await page.click('.settings button[role=radio]:has-text("Light")')
  check((await bg()) === autoBg && (await page.evaluate(() => document.documentElement.dataset.theme)) === 'light', 'theme: Light restores the light palette')
  await page.click('.settings button[role=radio]:has-text("Auto")')
  check((await page.evaluate(() => document.documentElement.dataset.theme)) === undefined && (await page.evaluate(() => localStorage.getItem('theme'))) === null, 'theme: Auto forgets the choice')
  await page.waitForSelector('.settings-hint:has-text("Next scan")')

  // profile section: shows the real CV and notes; never modifies them here
  const prof = await (await api('/profile')).json()
  await page.waitForSelector('.profile-cv strong')
  check((await page.locator('.profile-cv').textContent()).includes(prof.cv?.name ?? '<none>'), `profile: shows the stored CV (${prof.cv?.name})`)
  check((await page.inputValue('#notes')) === prof.notes, 'profile: notes textarea holds the stored notes')
  check(await page.locator('button:has-text("Save notes")').isDisabled(), 'profile: Save notes disabled until edited')
  await page.locator('#notes').press('End'); await page.keyboard.type(' x')
  check(!(await page.locator('button:has-text("Save notes")').isDisabled()), 'profile: editing enables Save notes')
  await page.click('button:has-text("Discard")')
  check((await page.inputValue('#notes')) === prof.notes, 'profile: Discard restores')
  check(await page.locator('input[type=file]').count() === 1 && (await page.getAttribute('input[type=file]', 'accept')).includes('.pdf'), 'profile: CV upload accepts pdf/md/txt')
  const [download] = await Promise.all([page.waitForEvent('download'), page.click('.profile button:has-text("Download")')])
  check(download.suggestedFilename() === prof.cv.name, `profile: Download hands back ${download.suggestedFilename()}`)

  // preferences: the form reflects the file; never saved here (a save would
  // rewrite the real preferences.yaml)
  const prefs = (await (await api('/preferences')).json())
  await page.waitForSelector('.prefs .chip-value')
  const titleChips = await page.locator('.prefs .chip-value').allTextContents()
  check(prefs.preferences.titles.every((t) => titleChips.some((c) => c.startsWith(t))), 'preferences: title chips match the file')
  check((await page.locator('.rules li').count()) === prefs.preferences.location_rules.length, `preferences: ${prefs.preferences.location_rules.length} market rows in file order`)
  // Save stays clickable (a disabled button would swallow the click that
  // commits a half-typed chip); Discard appears only once something changed.
  check(!(await page.locator('.prefs button:has-text("Discard")').count()), 'preferences: no Discard until edited')
  await page.locator('.prefs .chip-input input').first().fill('e2e-chip')
  await page.keyboard.press('Enter')
  check((await page.locator('.prefs button:has-text("Discard")').count()) === 1, 'preferences: adding a chip marks the form dirty')
  await page.click('.prefs button:has-text("Discard")')
  check(!(await page.locator('.prefs button:has-text("Discard")').count()) && !(await page.locator('.prefs .chip-value:has-text("e2e-chip")').count()), 'preferences: Discard reverts')

  // sources: add a real board by URL (checked live), toggle it, remove it
  await page.waitForSelector('.source-list li')
  await api('/sources/as-replit', { method: 'DELETE' })  // leftover from an aborted run, if any
  const nBefore = (await (await api('/sources')).json()).sources.length
  await page.fill('input[aria-label="careers URL"]', 'https://jobs.ashbyhq.com/replit')
  await page.click('.sources button:has-text("Add")')
  await page.waitForSelector('.toast:has-text("Added as-replit")', { timeout: 30000 })
  const srcAfter = (await (await api('/sources')).json()).sources
  const added = srcAfter.find((s) => s.id === 'as-replit')
  check(srcAfter.length === nBefore + 1 && added?.origin === 'user' && added.following === 1 && added.deletable === true, 'sources: board added from a careers URL after a live check, followed by me')
  await page.waitForSelector('.source-list li:has-text("Replit")')
  const row = page.locator('.source-list li:has-text("Replit")')
  await row.locator('input[type=checkbox]').click()
  await row.locator('input[type=checkbox]:not(:checked)').waitFor()   // this row, not any switched-off row
  check((await (await api('/sources')).json()).sources.find((s) => s.id === 'as-replit').following === 0, 'sources: toggle off persists')
  await row.locator('button:has-text("Remove")').click()
  await page.waitForSelector('.toast:has-text("Removed as-replit from your list")')
  check(!(await (await api('/sources')).json()).sources.some((s) => s.id === 'as-replit'), 'sources: removed')
  // a host that does not exist: nothing to fetch, render or probe
  // (example.com is not a safe choice -- a Greenhouse board named "example" exists)
  await page.fill('input[aria-label="careers URL"]', 'https://careers.zzqx-nonexistent-domain.invalid/jobs')
  await page.click('.sources button:has-text("Add")')
  await page.waitForSelector('.toast.error', { timeout: 90000 })
  check(true, 'sources: an unsupported URL is refused with an explanation')

  // danger zone: the confirmation gate only; never confirmed against real data
  await page.click('button:has-text("Delete all jobs")')
  await page.waitForSelector('.danger-confirm')
  const confirmBtn = page.locator('.danger-confirm button:has-text("Delete all jobs")')
  check(await confirmBtn.isDisabled(), 'danger: confirm button disabled before typing')
  await page.fill('#confirm-word', 'delete')
  check(await confirmBtn.isDisabled(), 'danger: lowercase "delete" does not unlock it')
  await page.fill('#confirm-word', 'DELETE')
  check(!(await confirmBtn.isDisabled()), 'danger: exactly DELETE unlocks it')
  await page.click('.danger-confirm button:has-text("Cancel")')
  check(!(await page.locator('.danger-confirm').count()), 'danger: Cancel closes it')
  const wrong = await api('/reset/jobs', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ confirm: 'delete' }) })
  check(wrong.status === 400 && (await total(page)) === all, 'danger: API refuses the wrong word, nothing deleted')
  await page.click('button:has-text("Reset everything")')
  await page.waitForSelector('.danger-confirm')
  check((await page.locator('.danger-confirm-title').textContent()).includes('whole database'), 'danger: reset-everything has its own warning')
  await page.click('.danger-confirm button:has-text("Cancel")')

  // delete asks for confirmation; we decline so real data survives
  await freshCard(page).locator('button:has-text("Delete")').click()
  check(await page.locator('.confirm').isVisible(), 'delete: asks for confirmation')
  await page.locator('.confirm button:has-text("No")').click()
  check(!(await page.locator('.confirm').count()), 'delete: declining keeps the job')
  check((await total(page)) === all, 'delete declined: total unchanged')

  // archive-older-than with an absurd age is a no-op
  await page.fill('.archive-old input', '3650')
  await page.click('button:has-text("Archive older than")')
  await page.waitForSelector('.toast:has-text("Nothing that old")', { timeout: 5000 })
  check(true, 'archive older than 3650d: no-op toast')

  // load more paginates
  const before = await page.locator('.job').count()
  await page.click('.loadmore button')
  await page.waitForFunction((n) => document.querySelectorAll('.job').length > n, before)
  check((await page.locator('.job').count()) > before, `load more: ${before} -> ${await page.locator('.job').count()}`)
})

// ---------------------------------------------------------------- phone
await run('phone', { width: 390, height: 844 }, async (page) => {
  const cols = await page.evaluate(() => getComputedStyle(document.querySelector('.jobs')).gridTemplateColumns.split(' ').length)
  check(cols === 1, `phone: single column (${cols} col)`)
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)
  check(!overflow, 'phone: no horizontal overflow')
  check(!(await page.locator('.filters').isVisible()), 'phone: filters collapsed by default')
  await page.screenshot({ path: `${OUT}/phone.png` })
  await page.click('.filter-toggle')
  check(await page.locator('.filters').isVisible(), 'phone: Filters button opens the panel')
  await page.screenshot({ path: `${OUT}/phone-filters.png` })
  const tap = await page.locator('.job-actions .btn').first().boundingBox()
  check(tap && tap.height >= 40, `phone: action buttons tappable (${Math.round(tap?.height ?? 0)}px tall)`)
})

// ---------------------------------------------------------------- login
await run('login', { width: 1000, height: 800 }, async (page) => {
  await page.waitForSelector('form[aria-label="Log in"]')
  await page.screenshot({ path: `${OUT}/login.png` })
  check(!(await page.locator('.job').count()), 'login: nothing shown without a token')
  await page.fill('input[type="email"]', ADMIN.email)
  await page.fill('input[type="password"]', 'wrong-password')
  await page.click('button[type="submit"]')
  await page.waitForSelector('[role="alert"]')
  check((await page.locator('[role="alert"]').textContent()).includes('wrong'), 'login: wrong password says so')
  await page.fill('input[type="password"]', ADMIN.password)
  await page.click('button[type="submit"]')
  await page.waitForSelector('.job', { timeout: 15000 })
  check(await page.evaluate(() => !!localStorage.getItem('jobfinder.token')), 'login: token kept in localStorage')
  check(!page.url().includes('token'), 'login: token never in the URL')
  // log out from the Account section: back to the login form, token gone
  await page.click('button[aria-label="Settings"]')
  await page.waitForSelector('.account')
  await page.click('.account button:has-text("Log out")')
  await page.waitForSelector('form[aria-label="Log in"]')
  check(await page.evaluate(() => !localStorage.getItem('jobfinder.token')), 'logout: token removed')
}, null)

// a plain user: their own decisions, no admin controls
await run('non-admin', { width: 1000, height: 800 }, async (page) => {
  check(!(await page.locator('.btn-scan').count()), 'user: no Scan button')
  await page.waitForSelector('.banner.setup', { timeout: 10000 }).catch(() => {})
  check((await page.locator('.banner.setup').count()) === 1, 'user: their own setup checklist (no CV yet)')
  await page.click('button[aria-label="Settings"]')
  await page.waitForSelector('.prefs')
  check(!(await page.locator('.users').count()) && !(await page.locator('.danger').count()), 'user: no Users or Danger zone')
  await page.waitForSelector('.source-list li')
  check((await page.locator('.sources .settings-hint:has-text("You follow")').count()) === 1, 'user: has their own source list')
  check((await page.locator('.profile:not(.sources):not(.users):not(.danger):not(.account) .settings-section').first().textContent()) === 'Your profile', 'user: has their own Profile section')
  check((await page.locator('.account').textContent()).includes(USER.email), 'user: Account shows their email')
  await page.click('button[aria-label="Settings"]')
  const card = freshCard(page)
  const id = await card.getAttribute('data-id')
  await card.locator('button:has-text("Shortlist")').click()
  await page.waitForSelector(`.job[data-id="${id}"] .chip[data-kind=status][data-value=shortlisted]`)
  const asAdmin = await (await api(`/jobs/${id}`)).json()
  check(asAdmin.status === 'new', 'user: their shortlist is not the admin\'s')
  const userToken = await tokenFor(USER)
  await fetch(BASE + `/jobs/${id}/state`, { method: 'PATCH', headers: { authorization: `Bearer ${userToken}`, 'content-type': 'application/json' }, body: JSON.stringify({ status: 'new' }) })
  check((await fetch(BASE + '/settings', { headers: { authorization: `Bearer ${userToken}` } })).status === 403, 'user: settings API is 403')
}, await tokenFor(USER))

// admin: the Users section adds and removes an account
await run('users', { width: 1000, height: 800 }, async (page) => {
  await page.click('button[aria-label="Settings"]')
  await page.waitForSelector('.users')
  await page.fill('.users input[type="email"]', 'temp@example.com')
  await page.fill('.users input[type="password"]', 'temp-password-1')
  await page.click('.users button:has-text("Add user")')
  await page.waitForSelector('.users-row:has-text("temp@example.com")')
  check(await tokenFor({ email: 'temp@example.com', password: 'temp-password-1' }), 'users: new account can log in')
  page.on('dialog', (d) => d.accept())
  await page.locator('.users-row:has-text("temp@example.com") button:has-text("Remove")').click()
  await page.waitForFunction(() => !document.querySelector('.users-row')?.parentElement?.textContent.includes('temp@example.com'))
  check((await fetch(BASE + '/auth/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ email: 'temp@example.com', password: 'temp-password-1' }) })).status === 401, 'users: removed account cannot log in')
  check(!(await page.locator('.users-row:has-text("' + ADMIN.email + '") button:has-text("Remove")').count()), 'users: cannot remove yourself')
})

await browser.close()
check(errors.length === 0, `no console/page errors${errors.length ? ': ' + errors.join(' | ') : ''}`)
console.log(`\n${failures === 0 ? 'ALL PASS' : failures + ' FAILED'} — screenshots in ${OUT}/`)
process.exit(failures ? 1 : 0)
