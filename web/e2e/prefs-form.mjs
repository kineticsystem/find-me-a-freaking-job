// Every field of the preferences form, end to end, against a running server.
// PUT /preferences is intercepted and its payload inspected, so the real
// config/preferences.yaml is never written. Clicks are real mouse clicks (no
// actionability bypass), so a disabled button swallowing a click would fail.
import { chromium } from 'playwright'

// Default to the throwaway test instance that `dock.sh <name> test` starts, never to the real one.
const BASE = process.env.E2E_BASE ?? 'http://127.0.0.1:8098'
let failures = 0
const check = (cond, msg) => { console.log(`${cond ? 'PASS' : 'FAIL'}  ${msg}`); if (!cond) failures++ }
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b)

const login = await fetch(BASE + '/auth/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ email: 'admin@example.com', password: 'demo-admin-password' }) })
const TOKEN = (await login.json()).token
const api = (path, init = {}) => fetch(BASE + path, { ...init, headers: { authorization: `Bearer ${TOKEN}`, ...(init.headers ?? {}) } })

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1000, height: 1400 } })
await ctx.addInitScript((t) => localStorage.setItem('jobfinder.token', t), TOKEN)
const page = await ctx.newPage()
const errors = []
page.on('pageerror', (e) => errors.push(e.message))
page.on('console', (m) => { if (m.type() === 'error' && !/status of 4\d\d/.test(m.text())) errors.push(m.text()) })

const puts = []
await page.route('**/preferences', (route) => {
  if (route.request().method() !== 'PUT') return route.continue()
  const body = JSON.parse(route.request().postData())
  puts.push(body)
  // echo back what the server would: the same preferences, numbered rules
  const prefs = { ...body, location_rules: body.location_rules.map((r, i) => ({ ...r, priority: i + 1 })), market_priority: body.location_rules.map((r) => r.country || r.region) }
  return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ preferences: prefs, config_path: '' }) })
})
await page.goto(BASE + '/'); await page.waitForSelector('.job')
await page.click('button[aria-label="Settings"]'); await page.waitForSelector('.prefs .chip-value')
const original = (await (await api('/preferences')).json()).preferences

const realClick = async (locator) => { await locator.scrollIntoViewIfNeeded(); const b = await locator.boundingBox(); await page.mouse.click(b.x + b.width / 2, b.y + b.height / 2) }
const saveBtn = page.locator('button:has-text("Save preferences")')
const clickSave = async () => { const n = puts.length; await realClick(saveBtn); await page.waitForSelector('.toast'); await page.waitForTimeout(150); return puts.length > n ? puts.at(-1) : null }
const chipInput = (label) => page.locator(`.prefs label:has-text("${label}") .chip-input input, .prefs .settings-label:has-text("${label}") + .chip-input input, .prefs .settings-label:has-text("${label}") + .settings-hint + .chip-input input`).first()
const chips = (label) => page.locator(`.prefs label:has-text("${label}") .chip-value, .prefs .settings-label:has-text("${label}") + .chip-input .chip-value, .prefs .settings-label:has-text("${label}") + .settings-hint + .chip-input .chip-value`).allTextContents().then((a) => a.map((t) => t.replace(/×$/, '')))

// 1. text field
await page.fill('.prefs-grid label:has-text("Based in") input', 'Denmark')
let sent = await clickSave()
check(sent?.based_in === 'Denmark', 'based_in: text field saved')

// 2. chip: type + Enter, then Save
await chipInput('Citizenship').fill('Malta'); await chipInput('Citizenship').press('Enter')
sent = await clickSave()
check(sent && sent.citizenship.includes('Malta'), 'citizenship: chip added with Enter, saved')

// 3. chip: type, click Save WITHOUT Enter  (the reported bug)
await chipInput('Must have').fill('Rust')
sent = await clickSave()
check(sent && sent.must_have.includes('Rust'), 'must_have: chip typed and Save clicked without Enter — saved in the same click')

// 4. chip: comma-separated entry adds several
await chipInput('Nice to have').fill('Go, Zig'); await chipInput('Nice to have').press('Enter')
sent = await clickSave()
check(sent && sent.nice_to_have.includes('Go') && sent.nice_to_have.includes('Zig'), 'nice_to_have: comma-separated entry adds two chips')

// 5. chip: add one, then remove it with ×
await chipInput('Dealbreakers').fill('e2e-dealbreaker'); await chipInput('Dealbreakers').press('Enter')
sent = await clickSave()
check(sent && sent.dealbreakers.includes('e2e-dealbreaker'), 'dealbreakers: chip added')
await realClick(page.locator('.prefs .chip-value:has-text("e2e-dealbreaker") button').first())
sent = await clickSave()
check(sent && !sent.dealbreakers.includes('e2e-dealbreaker'), 'dealbreakers: × removes the chip')

// 6. chip: duplicate is ignored
await chipInput('Titles').fill(original.titles[0]); await chipInput('Titles').press('Enter')
check((await chips('Titles')).filter((t) => t === original.titles[0]).length === 1, 'titles: a duplicate chip is not added twice')

// 7. languages chip
await chipInput('Languages').fill('Dutch'); await chipInput('Languages').press('Enter')
sent = await clickSave()
check(sent && sent.languages.includes('Dutch'), 'languages: chip saved')

// 8. textareas
await page.fill('#auth', 'Test authorisation text.')
sent = await clickSave()
check(sent?.work_authorization_notes === 'Test authorisation text.', 'work_authorization_notes: textarea saved')

// 9. seniority toggles
const junior = page.locator('.prefs .chip-btn:has-text("junior")')
const wasOn = (await junior.getAttribute('class')).includes(' on')
await realClick(junior)
sent = await clickSave()
check(sent && sent.seniority.includes('junior') !== wasOn, `seniority: toggle ${wasOn ? 'off' : 'on'} saved`)

// 10. salary: on (if it was off), amount, currency, period, then off
const atLeast = page.locator('.prefs label.inline input[type=checkbox]')
if (!(await atLeast.isChecked())) await realClick(atLeast)
await page.fill('.prefs input[aria-label="amount"]', '95000')
await page.selectOption('.prefs select[aria-label="currency"]', 'USD')
await page.selectOption('.prefs select[aria-label="period"]', 'month')
sent = await clickSave()
check(sent && eq(sent.min_salary, { amount: 95000, currency: 'USD', period: 'month' }), 'min_salary: amount, currency and period saved')
await realClick(page.locator('.prefs label.inline input[type=checkbox]'))
sent = await clickSave()
check(sent && sent.min_salary === null, 'min_salary: unchecking "at least" saves null')

// 11. location rules: edit, reorder, add, remove (reordering needs two rows)
if ((await page.locator('.rules li').count()) < 2) {
  await realClick(page.locator('button:has-text("+ Add a market")'))
  await page.fill('.rules li >> nth=-1 >> input[aria-label="market"]', 'Second Market')
  await clickSave()
}
const n0 = await page.locator('.rules li').count()
await page.fill('.rules li >> nth=0 >> input[aria-label="market"]', 'Canada')
await page.selectOption('.rules li >> nth=0 >> select[aria-label="remote"]', 'any')
await page.fill('.rules li >> nth=0 >> input[aria-label="note"]', 'test note')
sent = await clickSave()
check(sent && sent.location_rules[0].country === 'Canada' && sent.location_rules[0].remote === 'any' && sent.location_rules[0].note === 'test note', 'location_rules: market, remote and note edited')
await realClick(page.locator('.rules li >> nth=0 >> button[aria-label="move down"]'))
sent = await clickSave()
check(sent && sent.location_rules[1].country === 'Canada', 'location_rules: move down reorders')
await realClick(page.locator('.rules li >> nth=1 >> button[aria-label="move up"]'))
sent = await clickSave()
check(sent && sent.location_rules[0].country === 'Canada', 'location_rules: move up reorders back')
await realClick(page.locator('button:has-text("+ Add a market")'))
await page.fill(`.rules li >> nth=${n0} >> input[aria-label="market"]`, 'Japan')
sent = await clickSave()
check(sent && sent.location_rules.length === n0 + 1 && sent.location_rules.at(-1).country === 'Japan', 'location_rules: add a market')
await realClick(page.locator(`.rules li >> nth=${n0} >> button[aria-label="remove market"]`))
sent = await clickSave()
check(sent && sent.location_rules.length === n0, 'location_rules: remove a market')

// 12. nothing changed -> no request, a toast says so
const before = puts.length
await realClick(saveBtn); await page.waitForSelector('.toast:has-text("Nothing to save")')
check(puts.length === before, 'save with no changes: no request, "Nothing to save"')

// 13. Discard reverts to the last saved state
await page.fill('.prefs-grid label:has-text("Based in") input', 'Nowhere')
await realClick(page.locator('.prefs button:has-text("Discard")'))
check((await page.inputValue('.prefs-grid label:has-text("Based in") input')) === 'Denmark', 'Discard: reverts to last saved values')

check(errors.length === 0, `no console/page errors${errors.length ? ': ' + errors.join(' | ') : ''}`)
await browser.close()
const after = (await (await api('/preferences')).json()).preferences
check(eq(after, original), 'real preferences.yaml untouched by this test')
console.log(`\n${failures === 0 ? 'ALL PASS' : failures + ' FAILED'}`)
process.exit(failures ? 1 : 0)
