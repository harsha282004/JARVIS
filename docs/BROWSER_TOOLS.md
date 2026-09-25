# Browser tools (Phase 20)

Registered in `browser/tools.py` (`SPECS`). Every tool: pydantic schema with **extra keys forbidden**, control characters rejected, a permission category, a timeout, an `idempotent` flag (safe to retry), and a normalized result. `BrowserTools.call(name, args)` never raises.

## Result

```json
{"success": true,  "action": "click_element", "target": "Play button", "url": "https://…/watch", "verified": true, "message": "Clicked Play button."}
{"success": false, "action": "click_element", "target": "Download button", "url": "https://…", "verified": false, "error": "Element not found."}
```
Optional keys: `data` (structured; page-derived text is sanitized), `needs_confirmation` + `category` (held for a spoken yes), `recovered`, `untrusted` (data carries website text). URLs never include the query string.

## Tools

| Tool | Arguments | Category | Timeout | Retry | Verification |
|---|---|---|---|---|---|
| `open_url` | `url`, `new_tab=false` | navigation | 30 s | yes | host matches, status < 400, title present |
| `go_back`, `go_forward` | – | navigation | 30 s | no | URL changed |
| `refresh_page` | – | navigation | 30 s | yes | load finished |
| `open_new_tab` | `url?` | navigation | 30 s | no | tab count +1 |
| `switch_tab` | `tab_id` (`t1`…) | navigation | 10 s | yes | active tab is it |
| `close_tab` | `tab_id?` | navigation | 10 s | no | tab gone |
| `close_browser` | – | navigation | 30 s | no | state closed |
| `web_search` | `query` | navigation | 45 s | yes | results extracted; none opened; human-check reported |
| `get_page_state`, `get_page_title`, `get_current_url` | – | read | 10–15 s | yes | – |
| `read_page` | – | read | 20 s | yes | – (untrusted data, injection flag) |
| `find_text` | `text` | read | 15 s | yes | count > 0 |
| `find_element` | `description` | read | 15 s | yes | candidates (nothing clicked) |
| `wait_for_element` | `role?`, `name`/`text`, `timeout_seconds ≤ 30` | read | ≤ 45 s | yes | element present |
| `take_screenshot` | – | read | 20 s | yes | size known; refused on sign-in pages / `off` mode |
| `click_element` | `role?`, `name`/`text`, `index?` | interaction → external/sensitive by words | 30 s | **no** | page/tab/download changed |
| `type_text` | `role?`, `name`/`label`/`placeholder`, `text ≤ 500`, `submit=false` | interaction (external if submitting a non-search form) | 30 s | **no** | value read back; never password fields |
| `press_key` | `key` ∈ Enter, Escape, Tab, Space, arrows, Page/Home/End, k j l m f n | interaction (Enter = external) | 15 s | **no** | – (reports whether the page changed) |
| `scroll` | `direction` ∈ up/down/top/bottom, `amount` 50–5000 | interaction | 15 s | no | position moved or at the edge |
| `upload_file` | `label?`, `filename` (bare name in the uploads folder) | **sensitive** | 30 s | **no** | input holds the file; "not submitted" |
| `open_youtube` | – | navigation | 45 s | yes | youtube.com, title |
| `search_youtube` | `query` | navigation | 45 s | yes | result cards read |
| `play_youtube` | `query?`, `choice? 1–15`, `official=false` | interaction | 60 s | no | video playing (or ad), title matches |
| `pause_youtube`, `resume_youtube` | – | interaction | 20 s | yes | `<video>` paused / playing |
| `skip_youtube` | – | interaction | 30 s | no | ad gone / video changed |
| `seek_youtube` | `seconds ±600` | interaction | 20 s | no | time moved (refused during ads) |
| `volume_youtube` | `percent 0–100` | interaction | 20 s | yes | volume matches |
| `close_youtube` | – | navigation | 20 s | no | YouTube tabs gone |

`get_page_state` returns: title, tabs, loading, dialog, `login_required`, `captcha`, first headings, link/button counts, scroll position.

## Spoken commands (BrowserRouter)

"Open YouTube." · "Open YouTube and play Blinding Lights." · "Search for Blinding Lights." · "Play the official song / the second one / number 3." · "Pause." "Resume." "Skip." "Jump forward 30 seconds." "Volume to 40 percent." "Volume up." · "Close YouTube." · "Open GitHub." "Open my Virtual Campus repository." "Search for my Virtual Campus repository." (GitHub showing) "Open pull requests / issues / the latest issue." · "Open example dot com." "Go to github." · "Search the web for RAG architecture." · "Go back / forward." "Refresh the page." · "Scroll down / to the bottom." · "Click the download button." "Find the login page." "Type hello into the search field." "Press Enter." · "What page is this?" "Read the page." "Which tabs are open?" · "Close the tab." "Close the browser."

Not guessed: "Open my portfolio" → "I don't know a website called my portfolio. Tell me its address." "Play this" with several results → "Which one do you mean?"; with nothing showing → "What would you like me to play?". "Pause" with nothing playing is not a browser command at all.

## Python

```python
engine = BrowserEngine(driver_factory, BrowserConfig(...), BrowserLog(path))
tools = BrowserTools(engine)                       # registers the five categories with a PermissionManager
result = tools.call("open_url", {"url": "https://github.com/"})
```

## Phase 21 additions

* `upload_file.filename` must be a bare file name (`^[\w][\w .-]{0,118}$`): no path separators, checked by the schema before the engine's own confinement.
* YouTube playback is now judged only when the `<video>` is actually **not paused** (an ad that is still buffering no longer counts as playing).
* Autonomous tasks call these same tools by name through `autonomy.toolrouter.ToolRouter`; they add read-only tools of their own (GitHub repository search, README, deterministic analysis) documented in `AUTONOMOUS_AGENT_ARCHITECTURE.md`.
