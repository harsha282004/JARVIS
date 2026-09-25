"""Deterministic text analysis for autonomous tasks: README section summaries, technology detection, page-section checks, official-result ranking.

No language model, no network, no execution. Input is untrusted text (a README, a web page); output is short, plain, and never contains a
command line the README asked the reader to run: those lines are dropped and counted, so an attacker's "curl evil | sh" cannot be relayed as a suggestion.
"""

import re
from typing import Any
from urllib.parse import urlsplit

from browser.urlsafe import registrable

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_SETUP_HEADINGS = re.compile(r"\b(requirements?|prerequisites?|dependencies|installation|install|setup|set up|getting started|quick ?start|usage|running|configuration)\b", re.I)
_OVERVIEW_HEADINGS = re.compile(r"\b(about|overview|introduction|description|features|what is)\b", re.I)
_COMMANDISH = re.compile(r"(?:^|\s)(?:curl|wget|iwr|iex|invoke-\w+|powershell|pwsh|cmd\s*/c|sudo|chmod|rm\s+-\w+|del\s+/|format\s+\w:|bash|sh\s+-c|eval|nc\s+-)\b|\|\s*(?:sh|bash|iex|powershell)\b|>\s*/dev/", re.I)
_INSTALLISH = re.compile(r"^(?:\$\s*)?(?:pip3?|python3?|npm|yarn|pnpm|docker|docker-compose|git|conda|poetry|uv|cargo|go|make|mvn|gradle|alembic|uvicorn|flask|django-admin)\b", re.I)
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
_MD = re.compile(r"[`*_>]|!?\[([^\]]*)\]\([^)]*\)")

TECHNOLOGIES = {
    "python": "Python", "fastapi": "FastAPI", "flask": "Flask", "django": "Django", "sqlalchemy": "SQLAlchemy", "alembic": "Alembic", "postgresql": "PostgreSQL",
    "postgres": "PostgreSQL", "pgvector": "pgvector", "sqlite": "SQLite", "mysql": "MySQL", "mongodb": "MongoDB", "redis": "Redis", "docker": "Docker",
    "kubernetes": "Kubernetes", "react": "React", "next.js": "Next.js", "nextjs": "Next.js", "vue": "Vue", "angular": "Angular", "svelte": "Svelte",
    "typescript": "TypeScript", "javascript": "JavaScript", "node.js": "Node.js", "nodejs": "Node.js", "express": "Express", "tailwind": "Tailwind CSS",
    "java": "Java", "spring": "Spring", "kotlin": "Kotlin", "swift": "Swift", "rust": "Rust", "golang": "Go", "c++": "C++", "c#": "C#", ".net": ".NET",
    "pytorch": "PyTorch", "tensorflow": "TensorFlow", "scikit-learn": "scikit-learn", "pandas": "pandas", "numpy": "NumPy", "opencv": "OpenCV",
    "langchain": "LangChain", "ollama": "Ollama", "openai": "OpenAI API", "whisper": "Whisper", "playwright": "Playwright", "selenium": "Selenium",
    "graphql": "GraphQL", "firebase": "Firebase", "aws": "AWS", "azure": "Azure", "gcp": "Google Cloud", "supabase": "Supabase", "flutter": "Flutter",
    "html": "HTML", "css": "CSS", "bootstrap": "Bootstrap", "webpack": "Webpack", "vite": "Vite", "pytest": "pytest", "jwt": "JWT", "websocket": "WebSockets",
}


def _plain(text: str) -> str:
    return " ".join(_MD.sub(lambda m: m.group(1) or "", text).split())


def sections(markdown: str) -> list[tuple[str, list[str]]]:
    """[(heading, body lines)] with the text before the first heading under ''."""
    out: list[tuple[str, list[str]]] = [("", [])]
    in_code = False
    for line in markdown.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            out[-1][1].append(line)
            continue
        m = None if in_code else _HEADING.match(line)
        if m:
            out.append((_plain(m.group(2)), []))
        else:
            out[-1][1].append(line)
    return out


def _safe_line(line: str) -> bool:
    return not _COMMANDISH.search(line)


def summarize_readme(markdown: str, focus: str = "setup", max_items: int = 8) -> dict[str, Any]:
    """A short structured summary of the sections that answer `focus` ("setup" | "overview" | "technologies"). Command lines are never repeated."""
    secs = sections(markdown)
    pattern = _SETUP_HEADINGS if focus == "setup" else _OVERVIEW_HEADINGS
    chosen = [(h, body) for h, body in secs if h and pattern.search(h)]
    title = next((h for h, _ in secs if h), "")
    dropped = 0
    bullets: list[str] = []
    steps: list[str] = []
    prose: list[str] = []
    in_code = False
    for heading, body in chosen:
        for raw in body:
            stripped = raw.strip()
            if stripped.startswith("```"):
                in_code = not in_code
                continue
            if not stripped:
                continue
            if in_code:
                if not _safe_line(stripped):
                    dropped += 1
                elif _INSTALLISH.match(stripped) and len(steps) < max_items:
                    steps.append(stripped.lstrip("$ ").strip()[:120])
                continue
            m = _LIST_ITEM.match(raw)
            text = _plain(m.group(1) if m else stripped)
            if not text or not _safe_line(text):
                dropped += 1
                continue
            if m:
                if len(bullets) < max_items:
                    bullets.append(text[:140])
            elif len(prose) < 3 and not stripped.startswith(("|", "<", "!")):
                prose.append(text[:200])
            if _INSTALLISH.match(text) and len(steps) < max_items:
                steps.append(text[:120])
    if not chosen:  # no matching section: the opening paragraph is the best honest answer
        head = [_plain(l) for l in secs[0][1] + (secs[1][1] if len(secs) > 1 else []) if l.strip() and not l.strip().startswith(("#", "```", "|", "<", "!"))]
        prose = [h for h in head if _safe_line(h)][:2]
    return {"title": title, "headings": [h for h, _ in chosen], "bullets": bullets, "install_steps": steps, "prose": prose, "matched_section": bool(chosen),
            "commands_not_repeated": dropped, "empty": not (bullets or steps or prose)}


def format_summary(name: str, data: dict[str, Any], focus: str) -> str:
    """The spoken/dashboard wording of a README summary."""
    if data["empty"]:
        return f"I read the README for {name}, but it doesn't say anything about {'setup' if focus == 'setup' else 'that'}."
    label = "setup requirements" if focus == "setup" else "overview"
    parts = [f"The README for {name} lists these {label}:" if data["matched_section"] else f"The README for {name} doesn't have a clear {label} section. It begins:"]
    if data["bullets"]:
        parts.append("; ".join(data["bullets"][:6]) + ".")
    if data["prose"] and not data["bullets"]:
        parts.append(" ".join(data["prose"][:2]))
    if data["install_steps"]:
        parts.append("Setup steps mention: " + "; ".join(data["install_steps"][:4]) + ".")
    if data["commands_not_repeated"]:
        parts.append("It also contains some shell commands that I did not repeat or run.")
    return " ".join(parts)


def find_technologies(markdown: str, limit: int = 12) -> list[str]:
    text = markdown.lower()
    found: dict[str, int] = {}
    for key, label in TECHNOLOGIES.items():
        pattern = r"(?<![\w.+#-])" + re.escape(key) + r"(?![\w+#-])" if key not in (".net", "c++", "c#") else re.escape(key)
        n = len(re.findall(pattern, text))
        if n:
            found[label] = found.get(label, 0) + n
    return [name for name, _ in sorted(found.items(), key=lambda kv: -kv[1])][:limit]


def check_page_section(page: dict[str, Any], section: str, needle: str) -> dict[str, Any]:
    """Does the page have a heading `section`, and does `needle` appear on the page (headings, links, buttons, text)? Honest about what it cannot tell:
    the reading is flat text, so it cannot prove `needle` sits *inside* the section."""
    headings = [h.lower() for h in page.get("headings", [])]
    has_section = any(section.lower() in h for h in headings)
    words = [w for w in re.findall(r"[a-z0-9]+", needle.lower()) if len(w) > 1]
    hay = " ".join([*page.get("headings", []), page.get("text", ""), *(l.get("name", "") for l in page.get("links", [])), *page.get("buttons", [])]).lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", hay)
    mentioned = bool(words) and all(w in normalized for w in words)
    return {"has_section": has_section, "mentioned": mentioned, "section": section, "needle": needle}


_OFFICIAL_HINTS = ("docs.", "documentation", "developer.", "developers.", "readthedocs", ".org", "/docs", "/documentation", "/manual", "/reference", "/en/", "/guide")
_LOW_TRUST = ("medium.com", "quora.com", "reddit.com", "stackoverflow.com", "youtube.com", "w3schools.com", "geeksforgeeks.org", "tutorialspoint.com", "blogspot.", "wordpress.com",
              "pinterest.", "facebook.com", "linkedin.com")


def rank_official(results: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Order search results by how likely they are the project's OWN documentation: the technology name appears in the site's domain, doc-like paths,
    and not a known tutorial/blog/forum host. Returns copies with a `score`; the caller decides whether the top one is clear enough to open."""
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in {"the", "latest", "documentation", "docs", "official", "for", "of", "on", "a", "an", "and", "open", "most", "relevant", "result"}]
    ranked = []
    for i, r in enumerate(results):
        parts = urlsplit(r.get("url", ""))
        host = (parts.hostname or "").lower()
        site = registrable(host).split(".")[0] if host else ""
        score = 0.0
        if any(w == site for w in words):
            score += 0.6          # the site's own name IS the technology (postgresql.org for PostgreSQL)
        elif any((w in site or site in w) for w in words if len(site) > 2):
            score += 0.3          # a look-alike or third-party name that merely contains it (postgres.guide)
        if any(w in host for w in words):
            score += 0.1
        if any(h in host + parts.path.lower() for h in _OFFICIAL_HINTS):
            score += 0.2
        if any(bad in host for bad in _LOW_TRUST):
            score -= 0.5
        if any(w in r.get("title", "").lower() for w in words):
            score += 0.1
        score -= 0.01 * i
        ranked.append({**r, "score": round(score, 3)})
    return sorted(ranked, key=lambda r: -r["score"])
