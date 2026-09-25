"""Controlled browser agent (Phase 20). See docs/BROWSER_ARCHITECTURE.md.

    voice/text -> conversation -> BrowserRouter (deterministic patterns) -> BrowserTools (schema, category, PermissionManager,
    ConfirmationEngine) -> BrowserEngine (sessions, tabs, state, verification, recovery) -> PageDriver (Playwright) -> browser

Nothing in this package runs a shell, reads cookies/passwords, or executes model-supplied script.
"""
