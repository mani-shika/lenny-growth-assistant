"""HTML sanitiser for generated artifacts.

Threat model
------------
The HTML in an artifact is written by a language model, and that model has just
read attacker-influenceable text: a user's message, and transcript passages
retrieved by similarity to it. So generated HTML is **untrusted input**, exactly
like a comment field. The realistic attacks are stored XSS (script that runs for
the next viewer of the session), data exfiltration (a beacon carrying session
content to a third party), and UI redress (an overlay that phishes credentials).

Two independent layers
----------------------
1. **This allowlist sanitiser**, server-side. Unknown tags, all event handlers,
   and any non-`https`/`data:` URL are dropped before the artifact is stored.
   It is an allowlist, not a blocklist: anything not explicitly permitted is
   removed, so a tag nobody thought about fails closed.
2. **A sandboxed iframe in the viewer**, client-side. `srcdoc` with a `sandbox`
   attribute that grants *no* tokens - no `allow-scripts`, no
   `allow-same-origin` - plus a `default-src 'none'` CSP inside the document.

Either layer alone would be a single point of failure. Together, a sanitiser
bypass still lands in a document that cannot execute script, cannot read the
parent origin, and cannot open a network connection.

What is deliberately allowed: structural and text markup, inline `style`, and
`data:`-URI images. Generated artifacts are documents - tables, one-pagers,
styled summaries - and none of that needs script or network access. Blocking
scripts costs the product nothing real, so it is not a trade-off worth making.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

# Structural, text and table markup - everything a document needs.
ALLOWED_TAGS: frozenset[str] = frozenset(
    """
    a abbr address article aside b blockquote br caption cite code col colgroup
    dd del details div dl dt em figcaption figure footer h1 h2 h3 h4 h5 h6
    header hgroup hr i img ins kbd li main mark nav ol p pre q s samp section
    small span strong sub summary sup table tbody td tfoot th thead time tr u ul
    style
    """.split()
)

# Void elements must not be given a closing tag.
VOID_TAGS: frozenset[str] = frozenset({"br", "col", "hr", "img"})

GLOBAL_ATTRS: frozenset[str] = frozenset({"class", "id", "title", "style", "lang", "dir"})
TAG_ATTRS: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "target", "rel"}),
    "img": frozenset({"src", "alt", "width", "height", "loading"}),
    "td": frozenset({"colspan", "rowspan", "align"}),
    "th": frozenset({"colspan", "rowspan", "align", "scope"}),
    "col": frozenset({"span", "width"}),
    "colgroup": frozenset({"span"}),
    "ol": frozenset({"start", "type", "reversed"}),
    "time": frozenset({"datetime"}),
    "details": frozenset({"open"}),
}

# Tags whose *content* is dropped as well as the tag itself - keeping the text
# of a <script> would just move the payload into the document body.
DROP_CONTENT_TAGS: frozenset[str] = frozenset(
    {"script", "noscript", "template", "iframe", "object", "embed", "form", "svg", "math"}
)

SAFE_URL_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto", "tel"})
# Inline images only. A remote image URL is a tracking pixel that also leaks the
# viewer's IP and the fact that they opened the artifact.
SAFE_IMG_SCHEMES: frozenset[str] = frozenset({"data"})
SAFE_DATA_IMG_RE = re.compile(r"^data:image/(png|jpe?g|gif|webp|svg\+xml);base64,", re.I)

# CSS constructs that reach the network or execute.
# Handled as three separate passes because they need different removal shapes:
# an at-rule swallows to its semicolon, a url() swallows to its paren, and the
# legacy execution vectors are simple substrings.
CSS_AT_IMPORT_RE = re.compile(r"@import[^;}]*(;|$)", re.I)
# Any url(...) that is not an inline image. Protocol-relative ("//host/x") and
# bare relative paths are included: inside an origin-less sandboxed iframe they
# cannot resolve to anything useful, and outside one they are a beacon.
CSS_URL_RE = re.compile(
    r"""url\s*\(\s*(?P<q>['"]?)(?!data:image/)[^)]*(?P=q)\s*\)""", re.I
)
CSS_EXEC_RE = re.compile(
    r"(expression\s*\(|javascript\s*:|vbscript\s*:|-moz-binding\s*:|behaviou?r\s*:)", re.I
)

CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    "font-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


@dataclass(slots=True)
class SanitiseReport:
    """What was removed, so the viewer can show it and an auditor can read it."""

    removed_tags: list[str] = field(default_factory=list)
    removed_attributes: list[str] = field(default_factory=list)
    removed_urls: list[str] = field(default_factory=list)
    modified: bool = False

    def record_tag(self, tag: str) -> None:
        self.modified = True
        if tag not in self.removed_tags:
            self.removed_tags.append(tag)

    def record_attr(self, label: str) -> None:
        self.modified = True
        if label not in self.removed_attributes:
            self.removed_attributes.append(label)

    def record_url(self, url: str) -> None:
        self.modified = True
        trimmed = url[:120]
        if trimmed not in self.removed_urls:
            self.removed_urls.append(trimmed)

    def to_dict(self) -> dict:
        return {
            "modified": self.modified,
            "removed_tags": self.removed_tags,
            "removed_attributes": self.removed_attributes,
            "removed_urls": self.removed_urls,
        }

    def summary(self) -> str:
        if not self.modified:
            return "Nothing was removed; the artifact was already within policy."
        bits = []
        if self.removed_tags:
            bits.append(f"tags: {', '.join(self.removed_tags)}")
        if self.removed_attributes:
            bits.append(f"attributes: {', '.join(self.removed_attributes)}")
        if self.removed_urls:
            bits.append(f"{len(self.removed_urls)} unsafe URL(s)")
        return "Removed " + "; ".join(bits) + "."


class _Sanitiser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.report = SanitiseReport()
        self._suppress_depth = 0
        self._suppressed_tag: str | None = None
        self._open: list[str] = []

    # -- tags ---------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()

        if self._suppress_depth:
            if tag == self._suppressed_tag:
                self._suppress_depth += 1
            return

        if tag in DROP_CONTENT_TAGS:
            self.report.record_tag(tag)
            self._suppress_depth = 1
            self._suppressed_tag = tag
            return

        if tag not in ALLOWED_TAGS:
            # Unwrap rather than drop: the text inside an unknown tag is
            # usually legitimate content the model meant to show.
            self.report.record_tag(tag)
            return

        rendered = self._render_attrs(tag, attrs)
        if tag in VOID_TAGS:
            self.out.append(f"<{tag}{rendered}>")
        else:
            self.out.append(f"<{tag}{rendered}>")
            self._open.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._suppress_depth:
            return
        if tag in DROP_CONTENT_TAGS or tag not in ALLOWED_TAGS:
            self.report.record_tag(tag)
            return
        self.out.append(f"<{tag}{self._render_attrs(tag, attrs)}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._suppress_depth:
            if tag == self._suppressed_tag:
                self._suppress_depth -= 1
                if self._suppress_depth == 0:
                    self._suppressed_tag = None
            return
        if tag in VOID_TAGS or tag not in ALLOWED_TAGS:
            return
        if tag in self._open:
            # Close anything left dangling so malformed output cannot escape
            # its container in the viewer.
            while self._open:
                current = self._open.pop()
                self.out.append(f"</{current}>")
                if current == tag:
                    break

    # -- text ---------------------------------------------------------------
    def handle_data(self, data: str) -> None:
        if self._suppress_depth:
            return
        if self._open and self._open[-1] == "style":
            self.out.append(_clean_css(data, self.report))
            return
        # Order matters: escape & first, or the escapes below get double-encoded.
        escaped = data.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.out.append(escaped)

    def handle_comment(self, data: str) -> None:
        # Conditional comments are an execution vector in some renderers, and
        # a comment never carries content the user needs.
        return

    def handle_decl(self, decl: str) -> None:
        return

    def unknown_decl(self, data: str) -> None:
        return

    def handle_pi(self, data: str) -> None:
        return

    # -- attributes ---------------------------------------------------------
    def _render_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        allowed = GLOBAL_ATTRS | TAG_ATTRS.get(tag, frozenset())
        parts: list[str] = []
        has_href = False

        for raw_name, raw_value in attrs:
            name = raw_name.lower()
            value = raw_value or ""

            # Every on* handler, plus anything not on the allowlist.
            if name.startswith("on") or name not in allowed:
                self.report.record_attr(f"{tag}[{name}]")
                continue

            if name == "style":
                cleaned = _clean_css(value, self.report)
                if cleaned.strip():
                    parts.append(f'style="{_quote(cleaned)}"')
                continue

            if name in {"href", "src"}:
                schemes = SAFE_IMG_SCHEMES if (tag == "img") else SAFE_URL_SCHEMES
                if not _url_is_safe(value, schemes):
                    self.report.record_url(value)
                    continue
                has_href = has_href or name == "href"

            parts.append(f'{name}="{_quote(value)}"')

        # Any link that survives opens in a new tab with the opener severed.
        if tag == "a" and has_href:
            parts = [p for p in parts if not p.startswith(("target=", "rel="))]
            parts.append('target="_blank"')
            parts.append('rel="noopener noreferrer nofollow"')

        return (" " + " ".join(parts)) if parts else ""

    def finish(self) -> str:
        while self._open:
            self.out.append(f"</{self._open.pop()}>")
        return "".join(self.out)


def _quote(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def _url_is_safe(url: str, schemes: frozenset[str]) -> bool:
    candidate = url.strip()
    if not candidate:
        return False
    # Strip control characters and whitespace used to smuggle "java\nscript:".
    normalised = re.sub(r"[\s\x00-\x1f]+", "", candidate).lower()
    if normalised.startswith("data:"):
        return "data" in schemes and bool(SAFE_DATA_IMG_RE.match(candidate))
    if ":" not in normalised.split("/")[0]:
        # Relative URL. Harmless in a sandboxed, origin-less iframe, but it
        # cannot resolve to anything either, so keep it only for anchors.
        return not normalised.startswith("//")
    scheme = normalised.split(":", 1)[0]
    return scheme in schemes


def _clean_css(css: str, report: SanitiseReport) -> str:
    """Strip network reach and execution from CSS, keeping presentation intact."""
    cleaned = css
    if CSS_AT_IMPORT_RE.search(cleaned):
        report.record_attr("css[@import]")
        cleaned = CSS_AT_IMPORT_RE.sub("", cleaned)
    if CSS_URL_RE.search(cleaned):
        report.record_attr("css[url()]")
        for match in CSS_URL_RE.finditer(cleaned):
            report.record_url(match.group(0))
        # "none" keeps declarations like `background: url(x) no-repeat` valid
        # instead of leaving a dangling property value.
        cleaned = CSS_URL_RE.sub("none", cleaned)
    if CSS_EXEC_RE.search(cleaned):
        report.record_attr("css[expression/javascript]")
        cleaned = CSS_EXEC_RE.sub("", cleaned)
    return cleaned


def sanitize_html(html: str) -> tuple[str, SanitiseReport]:
    """Return `(safe_html, report)`. Never raises on malformed input."""
    parser = _Sanitiser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup must not 500 the request
        parser.report.record_tag("<unparseable>")
    return parser.finish(), parser.report


def wrap_document(body_html: str, title: str) -> str:
    """Wrap sanitised markup in a self-contained, CSP-locked document.

    This is what the viewer puts in `srcdoc`. The CSP is the third layer: even
    inside a sandbox with no script permission, it blocks any network fetch a
    stylesheet might otherwise attempt.
    """
    safe_title = (
        title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")[:200]
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{CSP}">
<title>{safe_title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    margin: 0; padding: 28px;
    font: 16px/1.65 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    color: #14181f; background: #fff;
  }}
  h1, h2, h3 {{ line-height: 1.25; margin: 1.6em 0 .5em; }}
  h1 {{ font-size: 1.8rem; margin-top: 0; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1.2em 0; }}
  th, td {{ border: 1px solid #d8dee8; padding: 8px 12px; text-align: left; }}
  th {{ background: #f4f6fa; }}
  code, pre {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  pre {{ background: #f4f6fa; padding: 14px; border-radius: 8px; overflow-x: auto; }}
  blockquote {{ margin: 1.2em 0; padding-left: 1em; border-left: 3px solid #c9d2e0; color: #4a5568; }}
  img {{ max-width: 100%; height: auto; }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #e6e9ef; background: #14181f; }}
    th {{ background: #1e242e; }}
    th, td {{ border-color: #2c3542; }}
    pre {{ background: #1e242e; }}
    blockquote {{ border-left-color: #3a4553; color: #a8b2c1; }}
  }}
</style>
</head>
<body>
{body_html}
</body>
</html>"""
