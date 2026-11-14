---
name: mac-control
description: Controlling the user's Mac and browser: opening apps, the user's own Chrome (banking, sites they're signed into), headless browsing, screenshots, AppleScript, shell. Grocery carts are NOT here: load the groceries skill.
---

macOS laptop control — use these to interact with the Mac directly:
- mac_open: launch a native app (Spotify, Finder, Terminal). NOT for websites.
- mac_chrome_navigate: open a URL in the user's existing Chrome (keeps login sessions).
  Use this for banking or any site the user is already signed into there. NOT for
  grocery carts: that's Remy (trigger_grocery_run, groceries skill), whose own
  browser holds the H-E-B / Target / Amazon logins. The user's Chrome is often
  not signed in to those.
- mac_chrome_read: read visible text from the current Chrome tab.
- mac_chrome_js: execute JavaScript in Chrome to click buttons, fill fields, read elements.
  This is your primary tool for web automation on signed-in sites.
- browser_* (MCP Playwright): headless fresh-session browser for anonymous browsing,
  scraping, and testing (browser_navigate, browser_snapshot, browser_click, browser_type…).
  One live browser keeps page state across your calls. NOT signed in to anything —
  for logged-in sites always use mac_chrome_* instead.
- mac_screenshot, mac_applescript, mac_clipboard, mac_shell, mac_spotlight, mac_type, mac_notify.

Web automation workflow for browser tasks:
  1. mac_chrome_navigate to the site URL
  2. mac_chrome_read to understand the page structure
  3. mac_chrome_js to interact (search, add to cart, etc.)
  4. Repeat read → js → read to verify each step
