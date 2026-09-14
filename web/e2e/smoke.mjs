// End-to-end smoke test against a running server (default http://127.0.0.1:8099).
// Exercises every user action reversibly: nothing in the database is left changed.
//   pnpm e2e            (server must be up: `python -m jobfinder serve`)
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.env.E2E_BASE ?? 'http://127.0.0.1:8099'
const OUT = 'e2e/screenshots'
mkdirSync(OUT, { recursive: true })

let failures = 0
const check = (cond, msg) => {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${msg}`)
  if (!cond) failures++
}

const browser = await chromium.launch()
const errors = []

async function run(name, viewport, body) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  page.on('pageerror', (e) => errors.push(`${name}: ${e.message}`))
  // 4xx responses the suite provokes on purpose (a refused URL) are logged by
  // the browser as console errors; they are asserted elsewhere, not bugs.
  page.on('console', (m) => { if (m.type() === 'error' && !/status of 4\d\d/.test(m.text())) errors.push(`${name}: ${m.text()}`) })
  await page.goto(BASE + '/')
  await page.waitForSelector('.job', { timeout: 15000 })
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

// ---------------------------------------------------------------- desktop
await run('desktop', { width: 1280, height: 800 }, async (page) => {
  const all = await total(page)
  check(all > 0, `desktop: lists ${all} jobs`)
  check((await page.locator('.job').count()) === Math.min(30, all), 'desktop: first page renders 30 cards')
  const cols = await page.evaluate(() => getComputedStyle(document.querySelector('.jobs')).gridTemplateColumns.split(' ').length)
  check(cols === 2, `desktop: two-column grid (${cols} cols)`)
  await page.screenshot({ path: `${OUT}/desktop.png` })

  // search narrows and matches
  await page.fill('input[type=search]', 'python')
  await waitTotalNot(page, all)
  const hits = await total(page)
  check(hits > 0 && hits < all, `search "python": ${hits} of ${all}`)
  const texts = await page.locator('.job').allTextContents()
  check(texts.every((t) => /python/i.test(t)), 'search: every visible card mentions python (title/company/location/description)')
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
  const first = page.locator('.job').first()
  const title = await first.locator('.job-title').textContent()
  await first.locator('button:has-text("Archive")').click()
  await page.waitForFunction((t) => !document.querySelector('.job .job-title')?.textContent.includes(t), title)
  check(!(await page.locator('.job-title').allTextContents()).includes(title), 'archive: card leaves the active list')
  check((await total(page)) === all - 1, 'archive: total decrements')
  await page.selectOption(`.filters select >> nth=${SELECT.status}`, 'archived')
  await page.waitForSelector('.job.is-archived')
  const archivedTitles = await page.locator('.job.is-archived .job-title').allTextContents()
  check(archivedTitles.includes(title), 'status=archived: shows the archived card')
  await page.locator('.job.is-archived', { hasText: title }).locator('button:has-text("Unarchive")').click()
  // other jobs may genuinely be archived; only this card must leave the view
  await page.waitForFunction((t) => ![...document.querySelectorAll('.job.is-archived .job-title')].some((e) => e.textContent.includes(t)), title)
  check(true, 'unarchive: leaves the archived view')
  await page.selectOption(`.filters select >> nth=${SELECT.status}`, '')
  await waitTotal(page, all)
  check((await total(page)) === all, 'unarchive: total restored')

  // shortlist -> applied -> reset round trip
  const card = page.locator('.job').first()
  const t2 = await card.locator('.job-title').textContent()
  // other jobs may genuinely be shortlisted or applied; wait on this card only
  const thisCard = page.locator('.job', { hasText: t2 })
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
  const d = page.locator('.job').first()
  const dTitle = await d.locator('.job-title').textContent()
  await d.locator('button:has-text("Not for me")').click()
  check(await d.locator('.dismiss').isVisible(), 'not for me: asks why with chips')
  await d.locator('.chip-btn:has-text("agency / consultancy")').click()
  await d.locator('.chip-btn:has-text("on-site")').click()
  await d.locator('.dismiss input').fill('e2e test reason')
  await d.locator('.dismiss button:has-text("Not for me")').click()
  await page.waitForFunction((t) => !document.querySelector('.job .job-title')?.textContent.includes(t), dTitle)
  check((await total(page)) === all - 1, 'not for me: card leaves the active list')
  await page.selectOption(`.filters select >> nth=${SELECT.status}`, 'dismissed')
  await page.waitForSelector('.job-meta.reason')
  const shownReason = await page.locator('.job', { hasText: dTitle }).locator('.job-meta.reason').textContent()
  check(shownReason.includes('agency / consultancy') && shownReason.includes('on-site') && shownReason.includes('e2e test reason'),
        `dismissed view: reason shown (${shownReason.trim()})`)
  const rej = await (await fetch(BASE + '/rejections')).json()
  check(rej.rejections.some((r) => r.reason.includes('e2e test reason')), 'API /rejections: reason recorded for the model')
  await page.locator('.job', { hasText: dTitle }).locator('button:has-text("Restore")').click()
  await page.waitForFunction(() => document.querySelectorAll('.job-meta.reason').length === 0)
  await page.selectOption(`.filters select >> nth=${SELECT.status}`, '')
  await waitTotal(page, all)
  const rej2 = await (await fetch(BASE + '/rejections')).json()
  check(!rej2.rejections.some((r) => r.reason.includes('e2e test reason')), 'restore: reason cleared, total back to ' + all)

  // settings: change the scan interval from the UI, verify via the API and
  // the config file, restore the original value
  const origSettings = await (await fetch(BASE + '/settings')).json()
  await page.click('button[aria-label="Settings"]')
  await page.waitForSelector('.settings-hint:has-text("Next scan")')  // current values loaded
  const target = origSettings.interval_minutes === 90 ? 75 : 90  // anything different from the current value
  await page.selectOption('.settings-row select', 'minutes')
  await page.fill('#interval-n', String(target))
  await page.click('.settings button:has-text("Save")')
  await page.waitForSelector(`.toast:has-text("every ${target} minutes")`)
  const after = await (await fetch(BASE + '/settings')).json()
  check(after.interval_minutes === target, `settings: interval applied live (${origSettings.interval_minutes} -> ${after.interval_minutes})`)
  check(after.next_run && (new Date(after.next_run) - Date.now()) < (target + 1) * 60000, 'settings: next scan rescheduled within the new interval')
  await page.waitForFunction((t) => document.querySelector('.summary')?.textContent.includes(`every ${t} minutes`), target, { timeout: 10000 })
  check(true, 'settings: summary line shows the new interval')
  // profile section: shows the real CV and notes; never modifies them here
  const prof = await (await fetch(BASE + '/profile')).json()
  await page.waitForSelector('.profile-cv strong')
  check((await page.locator('.profile-cv').textContent()).includes(prof.cv?.name ?? '<none>'), `profile: shows the CV on disk (${prof.cv?.name})`)
  check((await page.inputValue('#notes')) === prof.notes, 'profile: notes textarea holds notes.md')
  check(await page.locator('button:has-text("Save notes")').isDisabled(), 'profile: Save notes disabled until edited')
  await page.locator('#notes').press('End'); await page.keyboard.type(' x')
  check(!(await page.locator('button:has-text("Save notes")').isDisabled()), 'profile: editing enables Save notes')
  await page.click('button:has-text("Discard")')
  check((await page.inputValue('#notes')) === prof.notes, 'profile: Discard restores')
  check(await page.locator('input[type=file]').count() === 1 && (await page.getAttribute('input[type=file]', 'accept')).includes('.pdf'), 'profile: CV upload accepts pdf/md/txt')

  // preferences: the form reflects the file; never saved here (a save would
  // rewrite the real preferences.yaml)
  const prefs = (await (await fetch(BASE + '/preferences')).json())
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
  await fetch(BASE + '/sources/as-replit', { method: 'DELETE' })  // leftover from an aborted run, if any
  const nBefore = (await (await fetch(BASE + '/sources')).json()).sources.length
  await page.fill('input[aria-label="careers URL"]', 'https://jobs.ashbyhq.com/replit')
  await page.click('.sources button:has-text("Add")')
  await page.waitForSelector('.toast:has-text("Added as-replit")', { timeout: 30000 })
  const srcAfter = (await (await fetch(BASE + '/sources')).json()).sources
  const added = srcAfter.find((s) => s.id === 'as-replit')
  check(srcAfter.length === nBefore + 1 && added?.origin === 'user' && added.enabled === 1, 'sources: board added from a careers URL after a live check')
  await page.waitForSelector('.source-list li:has-text("Replit")')
  const row = page.locator('.source-list li:has-text("Replit")')
  await row.locator('input[type=checkbox]').click()
  await page.waitForFunction(() => document.querySelector('.source-list li.off'))
  check((await (await fetch(BASE + '/sources')).json()).sources.find((s) => s.id === 'as-replit').enabled === 0, 'sources: toggle off persists')
  await row.locator('button:has-text("Remove")').click()
  await page.waitForSelector('.toast:has-text("Removed as-replit")')
  check(!(await (await fetch(BASE + '/sources')).json()).sources.some((s) => s.id === 'as-replit'), 'sources: removed')
  await page.fill('input[aria-label="careers URL"]', 'https://example.com/careers')
  await page.click('.sources button:has-text("Add")')
  await page.waitForSelector('.toast.error')
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
  const wrong = await fetch(BASE + '/reset/jobs', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ confirm: 'delete' }) })
  check(wrong.status === 400 && (await total(page)) === all, 'danger: API refuses the wrong word, nothing deleted')
  await page.click('button:has-text("Reset everything")')
  await page.waitForSelector('.danger-confirm')
  check((await page.locator('.danger-confirm-title').textContent()).includes('whole database'), 'danger: reset-everything has its own warning')
  await page.click('.danger-confirm button:has-text("Cancel")')

  await fetch(BASE + '/settings', { method: 'PATCH', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ interval_minutes: origSettings.interval_minutes }) })
  check((await (await fetch(BASE + '/settings')).json()).interval_minutes === origSettings.interval_minutes, 'settings: restored')

  // delete asks for confirmation; we decline so real data survives
  await page.locator('.job').first().locator('button:has-text("Delete")').click()
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

await browser.close()
check(errors.length === 0, `no console/page errors${errors.length ? ': ' + errors.join(' | ') : ''}`)
console.log(`\n${failures === 0 ? 'ALL PASS' : failures + ' FAILED'} — screenshots in ${OUT}/`)
process.exit(failures ? 1 : 0)
