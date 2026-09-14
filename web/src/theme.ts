export type Theme = 'auto' | 'dark' | 'light'

export function getTheme(): Theme {
  try {
    const t = localStorage.getItem('theme')
    return t === 'dark' || t === 'light' ? t : 'auto'
  } catch {
    return 'auto'
  }
}

/** Per browser, not per account: the phone and the desktop can differ. */
export function setTheme(theme: Theme): void {
  try {
    if (theme === 'auto') localStorage.removeItem('theme')
    else localStorage.setItem('theme', theme)
  } catch {
    /* private mode etc.: still applies for this page view */
  }
  if (theme === 'auto') delete document.documentElement.dataset.theme
  else document.documentElement.dataset.theme = theme
}
