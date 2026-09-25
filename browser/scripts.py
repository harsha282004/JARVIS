"""The ONLY JavaScript JARVIS ever runs in a page. Fixed strings written here, selected by name; no tool, user or model input is ever
interpolated into them or evaluated. All of them are read-only except MEDIA_COMMAND (play/pause/seek/volume on the page's <video>) and
SCROLL. None of them reads cookies, storage, or input values."""

SNAPSHOT = r"""
() => {
  const vis = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
                       return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const nameOf = (e) => clean(e.getAttribute('aria-label') || e.innerText || e.getAttribute('title') || e.getAttribute('alt') || '').slice(0, 120);
  const links = [...document.querySelectorAll('a[href]')].filter(vis).slice(0, 400)
      .map((a) => ({role: 'link', name: nameOf(a), href: a.href, tag: 'a'})).filter((x) => x.name).slice(0, 60);
  const buttons = [...document.querySelectorAll('button,[role=button],input[type=button],input[type=submit]')].filter(vis).slice(0, 300)
      .map((b) => ({role: 'button', name: clean(b.getAttribute('aria-label') || b.innerText || b.getAttribute('title') || '').slice(0, 120),
                    tag: b.tagName.toLowerCase(), enabled: !b.disabled})).filter((x) => x.name).slice(0, 40);
  const labelFor = (e) => { const id = e.id; let l = id ? document.querySelector('label[for="' + CSS.escape(id) + '"]') : null;
                            return clean((l && l.innerText) || e.getAttribute('aria-label') || e.getAttribute('placeholder') || e.name || '').slice(0, 80); };
  const fields = [...document.querySelectorAll('input,textarea,select')].filter((e) => e.type !== 'hidden' && vis(e)).slice(0, 40)
      .map((e) => ({role: e.tagName.toLowerCase() === 'select' ? 'combobox' : 'textbox', name: labelFor(e), tag: e.tagName.toLowerCase(),
                    input_type: (e.type || '').toLowerCase()}));
  const tables = [...document.querySelectorAll('table')].filter(vis).slice(0, 2)
      .map((t) => [...t.rows].slice(0, 10).map((r) => [...r.cells].slice(0, 6).map((c) => clean(c.innerText).slice(0, 60))));
  const root = document.querySelector('main') || document.body;
  const dialog = [...document.querySelectorAll('dialog[open],[role=dialog],[role=alertdialog]')].some(vis);
  const html = document.documentElement;
  const captcha = !!document.querySelector('iframe[src*="recaptcha"],iframe[src*="hcaptcha"],iframe[src*="challenges.cloudflare"],.g-recaptcha,.h-captcha') ||
                  /verify (that )?you are (a )?human|are you a robot|bots use|unusual traffic|complete the following challenge/i.test((document.body && document.body.innerText || '').slice(0, 3000));
  return {
    url: location.href, title: document.title || '',
    headings: [...document.querySelectorAll('h1,h2,h3')].filter(vis).slice(0, 30).map((h) => clean(h.innerText).slice(0, 120)).filter(Boolean).slice(0, 12),
    links, buttons, fields, tables,
    text: clean(root ? root.innerText : '').slice(0, 4000),
    has_password_field: !!document.querySelector('input[type=password]'),
    has_dialog: dialog, captcha, loading: document.readyState !== 'complete',
    scroll_y: Math.round(window.scrollY), scroll_max: Math.max(0, Math.round(html.scrollHeight - window.innerHeight)),
  };
}
"""

MEDIA_STATE = r"""
() => {
  const v = document.querySelector('video');
  const ad = !!document.querySelector('.ad-showing,.ytp-ad-player-overlay,.ytp-ad-text');
  const skipEl = document.querySelector('.ytp-skip-ad-button,.ytp-ad-skip-button,.ytp-ad-skip-button-modern');
  const skip = !!skipEl && skipEl.getBoundingClientRect().width > 0 && getComputedStyle(skipEl).visibility !== 'hidden' && getComputedStyle(skipEl).display !== 'none';
  if (!v) return {present: false, ad, skippable_ad: skip};
  return {present: true, paused: v.paused, current_time: v.currentTime || 0, duration: isFinite(v.duration) ? v.duration : 0,
          ended: v.ended, volume: v.volume, muted: v.muted, ad, skippable_ad: skip};
}
"""

MEDIA_COMMAND = r"""
([cmd, value]) => {
  const v = document.querySelector('video');
  if (!v) return false;
  if (cmd === 'play') { const p = v.play(); if (p && p.catch) p.catch(() => {}); return true; }
  if (cmd === 'pause') { v.pause(); return true; }
  if (cmd === 'seek') { v.currentTime = Math.max(0, Math.min(isFinite(v.duration) ? v.duration : 1e9, value)); return true; }
  if (cmd === 'volume') { v.volume = Math.max(0, Math.min(1, value)); v.muted = false; return true; }
  if (cmd === 'mute') { v.muted = !!value; return true; }
  return false;
}
"""

SCROLL = r"""
([dx, dy, mode]) => {
  if (mode === 'top') window.scrollTo(0, 0);
  else if (mode === 'bottom') window.scrollTo(0, document.documentElement.scrollHeight);
  else window.scrollBy(dx, dy);
  return [Math.round(window.scrollY), Math.max(0, Math.round(document.documentElement.scrollHeight - window.innerHeight))];
}
"""

YOUTUBE_RESULTS = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  return [...document.querySelectorAll('ytd-video-renderer')].slice(0, 15).map((r) => {
    const a = r.querySelector('a#video-title');
    const ch = r.querySelector('ytd-channel-name a, #channel-name a');
    const badges = [...r.querySelectorAll('ytd-badge-supported-renderer, .badge-style-type-verified-artist, .badge-style-type-verified')]
        .map((b) => clean(b.getAttribute('aria-label') || b.innerText)).filter(Boolean);
    const meta = [...r.querySelectorAll('#metadata-line span')].map((s) => clean(s.innerText));
    const dur = r.querySelector('ytd-thumbnail-overlay-time-status-renderer');
    return {title: clean(a && (a.getAttribute('title') || a.innerText)).slice(0, 200), href: a ? a.href : '',
            channel: clean(ch && ch.innerText).slice(0, 100), badges: badges.slice(0, 3), meta: meta.slice(0, 3),
            duration: clean(dur && dur.innerText).slice(0, 12)};
  }).filter((x) => x.title && x.href.includes('/watch'));
}
"""

WEB_RESULTS = r"""
() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const real = (h) => { try { const u = new URL(h, location.href); const t = u.searchParams.get('uddg'); return t ? decodeURIComponent(t) : u.href; } catch (e) { return ''; } };
  const rows = [];
  document.querySelectorAll('li.b_algo').forEach((r) => {                                   // Bing
    const a = r.querySelector('h2 a'); const s = r.querySelector('.b_caption p, .b_lineclamp2, p');
    if (a) rows.push({title: clean(a.innerText || a.textContent).slice(0, 160), url: a.href, snippet: clean(s && (s.innerText || s.textContent)).slice(0, 220)});
  });
  document.querySelectorAll('.result, .web-result').forEach((r) => {                        // DuckDuckGo html
    const a = r.querySelector('a.result__a, h2 a'); const s = r.querySelector('.result__snippet');
    if (a) rows.push({title: clean(a.innerText).slice(0, 160), url: real(a.href), snippet: clean(s && s.innerText).slice(0, 220)});
  });
  document.querySelectorAll('article[data-testid=result]').forEach((r) => {                 // DuckDuckGo main
    const a = r.querySelector('a[data-testid=result-title-a], h2 a'); const s = r.querySelector('[data-result=snippet], div[data-testid=result-snippet]');
    if (a) rows.push({title: clean(a.innerText).slice(0, 160), url: a.href, snippet: clean(s && s.innerText).slice(0, 220)});
  });
  return rows.filter((x) => x.title && x.url).slice(0, 12);
}
"""

TRUSTED = {"snapshot": SNAPSHOT, "media_state": MEDIA_STATE, "youtube_results": YOUTUBE_RESULTS, "web_results": WEB_RESULTS}
