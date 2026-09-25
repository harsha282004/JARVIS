"""URL validation: the first gate of every navigation.

Only http and https are ever opened. There is no "authorize a dangerous scheme" path: file:, javascript:, data:, vbscript:, blob:, ftp:,
chrome:, edge:, about: and everything else are refused. Hosts that point at this machine or its network (localhost, private and link-local
ranges including the cloud metadata address, numeric IP tricks such as 2130706433 or 0x7f000001, IPv4-mapped IPv6) are refused unless the
user explicitly turned `BROWSER_ALLOW_PRIVATE_HOSTS` on, and the name is resolved so a public-looking name that points at a private
address (DNS rebinding) is refused too. User info in a URL (https://user:pass@host) is refused: it is how phishing and credential leaks work.
"""

import ipaddress
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

MAX_URL_CHARS = 2048
_BLOCKED_PORTS = frozenset({21, 22, 23, 25, 110, 111, 135, 139, 143, 445, 1433, 2049, 3306, 3389, 5432, 5900, 6379, 9200, 11211, 27017})
_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".corp", ".intranet", ".localdomain")
_CONTROL = re.compile(r"[\x00-\x20\x7f​-‏ -‮⁠﻿]")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_LOOKS_LIKE_HOST = re.compile(r"^(?:[A-Za-z0-9\-]+\.)+[A-Za-z]{2,}(?:[:/?#].*)?$")
_SHORTENERS = frozenset({"bit.ly", "tinyurl.com", "t.co", "goo.gl", "is.gd", "ow.ly", "cutt.ly", "rb.gy", "shorturl.at", "tiny.cc"})

Resolver = Callable[[str], list[str]]


def system_resolver(host: str) -> list[str]:
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})
    except OSError:
        return []


@dataclass(frozen=True)
class UrlDecision:
    ok: bool
    url: str = ""
    host: str = ""
    reason: str = ""          # spoken to the user when not ok
    suspicious: str = ""      # allowed, but worth saying ("that's a link shortener")


def _numeric_host(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """An IP address written in any form a browser accepts (decimal, hex, octal, short dotted), or None."""
    bare = host.strip("[]")
    try:
        return ipaddress.ip_address(bare)
    except ValueError:
        pass
    if re.fullmatch(r"(?:0x[0-9a-fA-F]+|\d+)(?:\.(?:0x[0-9a-fA-F]+|\d+))*", bare):
        try:
            return ipaddress.ip_address(socket.inet_aton(bare))
        except OSError:
            return None
    return None


def _public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_unspecified)


def validate_url(raw: str, *, allow_private: bool = False, resolver: Resolver | None = system_resolver) -> UrlDecision:
    """Normalize and judge `raw`. A bare "github.com/x" becomes https. Never raises."""
    if not isinstance(raw, str) or not raw.strip():
        return UrlDecision(False, reason="There is no address to open.")
    text = raw.strip()
    if len(text) > MAX_URL_CHARS:
        return UrlDecision(False, reason="That address is too long.")
    if _CONTROL.search(text):
        return UrlDecision(False, reason="That address contains characters I won't open.")
    if not _SCHEME.match(text):
        if not _LOOKS_LIKE_HOST.match(text):
            return UrlDecision(False, reason="That doesn't look like a web address.")
        text = "https://" + text
    elif not re.match(r"^https?://", text, re.I):
        scheme = text.split(":", 1)[0].lower()
        return UrlDecision(False, reason=f"I only open http and https addresses, not {scheme}: links.")
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        return UrlDecision(False, reason="That isn't a valid web address.")
    if not host:
        return UrlDecision(False, reason="That address has no website name.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return UrlDecision(False, reason="I won't open addresses that contain a username or password.")
    if port is not None and (port in _BLOCKED_PORTS or port == 0):
        return UrlDecision(False, reason="I won't connect to that port.")
    try:
        ascii_host = host if host.isascii() else host.encode("idna").decode("ascii")
    except UnicodeError:
        return UrlDecision(False, reason="That website name isn't valid.")

    ip = _numeric_host(ascii_host)
    if ip is not None:
        if not allow_private and not _public(ip):
            return UrlDecision(False, reason="I won't open addresses on this computer or your local network.")
    else:
        if not allow_private and (ascii_host == "localhost" or ascii_host.endswith(_PRIVATE_SUFFIXES) or "." not in ascii_host):
            return UrlDecision(False, reason="I won't open addresses on this computer or your local network.")
        if not allow_private and resolver is not None:
            for address in resolver(ascii_host):
                try:
                    if not _public(ipaddress.ip_address(address.split("%")[0])):
                        return UrlDecision(False, reason="That website points at a private network address, so I won't open it.")
                except ValueError:
                    continue
    netloc = ascii_host + (f":{port}" if port else "")
    clean = urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))
    suspicious = ""
    if ascii_host in _SHORTENERS:
        suspicious = "That is a link shortener, so I can't tell where it leads."
    elif ascii_host.startswith("xn--") or ".xn--" in ascii_host:
        suspicious = "That website name uses look-alike characters."
    elif ip is not None:
        suspicious = "That is a raw IP address rather than a website name."
    return UrlDecision(True, clean, ascii_host, suspicious=suspicious)


def registrable(host: str) -> str:
    """The last two labels ("m.youtube.com" -> "youtube.com"); good enough to decide "same site" for reuse/verification."""
    labels = host.lower().rstrip(".").split(".")
    if len(labels) >= 3 and labels[-2] in ("co", "com", "org", "net", "gov", "ac") and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def same_site(a: str, b: str) -> bool:
    """Same website. Names compare by registrable domain (m.youtube.com = www.youtube.com); an IP address, or an address with a port, compares exactly:
    two local servers on different ports (or 127.0.0.1 vs 127.0.0.2) are different sites."""
    pa, pb = urlsplit(a), urlsplit(b)
    ha, hb = (pa.hostname or ""), (pb.hostname or "")
    if not (ha and hb):
        return False
    if _numeric_host(ha) is not None or _numeric_host(hb) is not None or pa.port or pb.port:
        return ha.lower() == hb.lower() and (pa.port or 0) == (pb.port or 0)
    return registrable(ha) == registrable(hb)


KNOWN_SITES: dict[str, str] = {
    "youtube": "https://www.youtube.com/", "google": "https://www.google.com/", "github": "https://github.com/", "gmail": "https://mail.google.com/",
    "google calendar": "https://calendar.google.com/", "google maps": "https://www.google.com/maps", "maps": "https://www.google.com/maps",
    "google drive": "https://drive.google.com/", "wikipedia": "https://www.wikipedia.org/", "stack overflow": "https://stackoverflow.com/",
    "stackoverflow": "https://stackoverflow.com/", "reddit": "https://www.reddit.com/", "linkedin": "https://www.linkedin.com/",
    "twitter": "https://x.com/", "x": "https://x.com/", "spotify": "https://open.spotify.com/", "amazon": "https://www.amazon.com/",
    "netflix": "https://www.netflix.com/", "hacker news": "https://news.ycombinator.com/", "duckduckgo": "https://duckduckgo.com/",
    "bing": "https://www.bing.com/", "chatgpt": "https://chatgpt.com/", "python docs": "https://docs.python.org/3/",
}
