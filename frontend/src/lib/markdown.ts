/** Markdown rendering, with sanitisation that is not optional.
 *
 *  Chat answers and Markdown artifacts are both model output, and model output
 *  is untrusted: it is written after reading the user's message and transcript
 *  passages retrieved by similarity to it. `marked` deliberately passes raw
 *  HTML through, so every conversion runs through DOMPurify before it reaches
 *  `dangerouslySetInnerHTML`.
 *
 *  Note the split with HTML artifacts: those never come through here. They are
 *  sanitised server-side and rendered inside a sandboxed iframe, because a full
 *  HTML document needs its own styling context and a much wider tag surface.
 *  Markdown's output surface is narrow enough that DOMPurify alone is
 *  proportionate. See docs/architecture.md, "Artifact security".
 */

import DOMPurify from "dompurify";
import { marked } from "marked";

marked.setOptions({ gfm: true, breaks: true });

/** Tags a Markdown document can legitimately produce. Nothing executable. */
const ALLOWED_TAGS = [
  "a", "blockquote", "br", "code", "del", "em", "h1", "h2", "h3", "h4", "h5",
  "h6", "hr", "img", "li", "ol", "p", "pre", "s", "span", "strong", "sub",
  "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul", "input",
];
const ALLOWED_ATTR = [
  "href", "title", "alt", "src", "class", "align", "colspan", "rowspan",
  "type", "checked", "disabled", "start",
];

// Force every surviving link to open safely. DOMPurify hooks run after the
// allowlist, so this cannot be bypassed by attribute ordering.
let hooked = false;
function installHooks(): void {
  if (hooked) return;
  DOMPurify.addHook("afterSanitizeAttributes", (node) => {
    if (node.tagName === "A" && node.hasAttribute("href")) {
      node.setAttribute("target", "_blank");
      node.setAttribute("rel", "noopener noreferrer nofollow");
    }
    // Task-list checkboxes render, but must never be interactive inputs.
    if (node.tagName === "INPUT") {
      node.setAttribute("disabled", "true");
    }
  });
  hooked = true;
}

export function renderMarkdown(source: string): string {
  installHooks();
  const raw = marked.parse(source ?? "", { async: false }) as string;
  return DOMPurify.sanitize(raw, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    // Block data:/blob: URIs outright - a Markdown answer has no reason to
    // embed one, and they are the usual way to smuggle an executable payload.
    ALLOWED_URI_REGEXP: /^(?:https?:|mailto:|tel:|#)/i,
  });
}

/**
 * Replace `[n]` citation markers with clickable superscripts.
 *
 * Runs on the *sanitised* HTML and only ever inserts a fixed, escaped element,
 * so it cannot reintroduce anything DOMPurify removed. Markers inside code
 * blocks are left alone - `array[0]` is not a citation.
 */
export function linkifyCitations(html: string, maxMarker: number): string {
  if (maxMarker <= 0) return html;
  const segments = html.split(/(<pre[\s\S]*?<\/pre>|<code[\s\S]*?<\/code>)/gi);
  return segments
    .map((segment, index) => {
      if (index % 2 === 1) return segment; // the captured code block
      return segment.replace(/\[(\d{1,2})\]/g, (match, digits: string) => {
        const marker = Number(digits);
        if (marker < 1 || marker > maxMarker) return match;
        return `<sup class="citation-marker" data-marker="${marker}" role="link" tabindex="0" aria-label="Source ${marker}">${marker}</sup>`;
      });
    })
    .join("");
}
