"""The Ship 30 for 30 essay skill.

The brief asks for the writing principles to be *encoded in the skill* rather
than stuffed into an ad-hoc prompt. So they live here as structured data, read
from the published Ship 30 for 30 material (sources listed in `SOURCES`), and
they are used twice:

1. **Composed into the prompt** - deterministically, in a fixed order, so the
   instruction block is byte-stable across turns and cacheable.
2. **Checked against the output** - `critique()` measures the draft against the
   same rules that produced it. A draft that misses them gets one targeted
   repair pass naming the specific failures, instead of a vague "try again".

That second use is the point. A prompt can only ask; a validator can tell you
whether you got it, which is what makes essay quality a measurable property
rather than a vibe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SOURCES = [
    "https://www.ship30for30.com/post/how-to-write-an-atomic-essay-a-beginners-guide",
    "https://www.ship30for30.com/post/6-proven-single-sentence-openers-to-hook-your-reader-s-attention",
    "https://www.ship30for30.com/post/flawless-formatting-a-step-by-step-guide-to-make-anything-you-write-easy-to-read-and-skimmable",
]

TARGET_WORDS = 1250
# The brief says "approximately 1,250 words". +/-20% is what a human editor
# would accept; outside it, the draft gets a repair pass.
WORD_FLOOR = 1000
WORD_CEILING = 1500


@dataclass(frozen=True, slots=True)
class Opener:
    name: str
    rule: str
    example: str


# The six single-sentence openers, from the Ship 30 for 30 hook guide.
OPENERS: tuple[Opener, ...] = (
    Opener(
        "Strong declarative sentence",
        "Plant your flag in the ground. No hedging.",
        "Being physically fit isn't a hobby, it's a lifestyle.",
    ),
    Opener(
        "Thought-provoking question",
        "Ask something the reader is already asking themselves.",
        "Is there such a thing as complete happiness?",
    ),
    Opener(
        "Controversial opinion",
        "Challenge conventional wisdom, but stay believable and defensible.",
        "ChatGPT is overused and overhyped.",
    ),
    Opener(
        "Moment in time",
        "Ground the reader in a specific date, time, scene or setting.",
        "In 1982, David Ogilvy wrote a memo titled 'How to write.'",
    ),
    Opener(
        "Vulnerable statement",
        "Share a real struggle, then connect it to what the reader gains.",
        "For the first 10 years of my career, I was a terrible husband.",
    ),
    Opener(
        "Weird, unique insight",
        "Lead with a surprising fact that provokes curiosity.",
        "Texas is not the largest state in the US. Alaska is.",
    ),
)

# The five things a Ship 30 headline has to do.
HEADLINE_ELEMENTS: tuple[str, ...] = (
    "who the piece is for",
    "what it is about",
    "how the reader should feel",
    "the outcome or promise",
    "how much information to expect",
)

FORMATTING_RULES: tuple[str, ...] = (
    "Open the piece, and every section, with a single sentence.",
    "If you are listing anything, ever, make it a bulleted list.",
    "Use bolded subheads to signal where the reader is in the argument; "
    "split the piece into roughly equal chunks so each section is a milestone.",
    "Use the 1/3/1 rhythm (or 1/4/1, 1/5/1): open with one clear sentence, "
    "build over the next few, close the point with one sentence.",
    "Bold only the line you would want a skimmer to read. Bolding everything "
    "bolds nothing.",
    "Keep paragraphs short and leave white space between them.",
)

NARRATIVE_ARC: tuple[str, ...] = (
    "Hook - one sentence, using one of the six opener types.",
    "Stakes - why this matters now, and to whom.",
    "Body - three to five sections, each a single idea with a bolded subhead.",
    "Evidence - concrete specifics from the transcripts, attributed to the guest.",
    "Takeaway - one specific, usable action, not a summary.",
)

CARDINAL_RULE = (
    "Deliver exactly what the headline promises. If the headline says "
    "'5 lessons', the essay contains exactly five."
)


def principles_block() -> str:
    """Render the encoded principles as the instruction block for the model."""
    openers = "\n".join(
        f"  {i}. {o.name} - {o.rule} e.g. \"{o.example}\""
        for i, o in enumerate(OPENERS, start=1)
    )
    formatting = "\n".join(f"  - {rule}" for rule in FORMATTING_RULES)
    arc = "\n".join(f"  {i}. {step}" for i, step in enumerate(NARRATIVE_ARC, start=1))
    headline = ", ".join(HEADLINE_ELEMENTS)

    return f"""SHIP 30 FOR 30 WRITING PRINCIPLES (follow all of them)

TITLE: a single H1. A good headline signals {headline}.

OPENING LINE: exactly one sentence, and it must be one of these six types:
{openers}

STRUCTURE:
{arc}

FORMATTING:
{formatting}

CARDINAL RULE: {CARDINAL_RULE}

LENGTH: approximately {TARGET_WORDS} words (accept {WORD_FLOOR}-{WORD_CEILING})."""


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

H1_RE = re.compile(r"^#\s+\S", re.M)
H2_RE = re.compile(r"^#{2,3}\s+\S", re.M)
BULLET_RE = re.compile(r"^\s*[-*+]\s+\S", re.M)
BOLD_RE = re.compile(r"\*\*[^*\n]{2,}\*\*")
CITATION_RE = re.compile(r"\[(\d{1,2})\]")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass(slots=True)
class Critique:
    """The result of measuring a draft against the encoded principles."""

    word_count: int
    failures: list[str]
    warnings: list[str]

    @property
    def passed(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict:
        return {
            "word_count": self.word_count,
            "passed": self.passed,
            "failures": self.failures,
            "warnings": self.warnings,
        }

    def repair_instruction(self) -> str:
        """A precise, addressable revision brief - never 'make it better'."""
        items = "\n".join(f"- {f}" for f in self.failures + self.warnings)
        return (
            "Your draft misses these required elements. Revise it, keeping every "
            "factual claim and every numbered citation exactly as it is, and output "
            "the full corrected essay only:\n"
            f"{items}"
        )


def word_count(text: str) -> int:
    """Count prose words, ignoring markdown syntax so formatting is not scored."""
    stripped = re.sub(r"[#*_`>\[\]()-]", " ", text)
    return len([w for w in stripped.split() if any(c.isalnum() for c in w)])


def first_sentence(body: str) -> str:
    """The first sentence of prose after the H1 title."""
    lines = [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines:
        return ""
    return SENTENCE_SPLIT_RE.split(lines[0])[0].strip()


def critique(text: str, *, require_citations: bool = True) -> Critique:
    """Measure a draft. `failures` block acceptance; `warnings` are advisory."""
    failures: list[str] = []
    warnings: list[str] = []
    words = word_count(text)

    if words < WORD_FLOOR:
        failures.append(
            f"Too short: {words} words. Expand to roughly {TARGET_WORDS} "
            f"(minimum {WORD_FLOOR}) by developing the existing sections with "
            "more specifics from the sources - do not add new claims."
        )
    elif words > WORD_CEILING:
        failures.append(
            f"Too long: {words} words. Cut to roughly {TARGET_WORDS} "
            f"(maximum {WORD_CEILING}) by tightening prose, not by deleting sections."
        )

    if not H1_RE.search(text):
        failures.append("No H1 title. Start with a single '# Headline' line.")

    headings = len(H2_RE.findall(text))
    if headings < 3:
        failures.append(
            f"Only {headings} section headings. Use at least 3 '## Subhead' "
            "sections so the piece is skimmable."
        )

    if not BULLET_RE.search(text):
        failures.append(
            "No bulleted list. Ship 30 rule: if you are listing anything, "
            "ever, it becomes a bulleted list."
        )

    bolds = len(BOLD_RE.findall(text))
    if bolds == 0:
        failures.append("No bold emphasis. Bold the lines a skimmer must read.")
    elif bolds > 25:
        warnings.append(
            f"{bolds} bolded spans is too many - bolding everything bolds nothing."
        )

    opener = first_sentence(text)
    if not opener:
        failures.append("No opening line found beneath the title.")
    elif len(opener.split()) > 35:
        warnings.append(
            "The opening line runs long. Ship 30 openers are a single, tight sentence."
        )

    if require_citations and not CITATION_RE.search(text):
        failures.append(
            "No numbered citations. Every claim drawn from the transcripts must "
            "carry its excerpt number in square brackets, like [3]."
        )

    return Critique(word_count=words, failures=failures, warnings=warnings)


def system_prompt() -> str:
    """The full system prompt for the essay skill."""
    return f"""You are a senior product-growth writer producing an essay in the \
Ship 30 for 30 style, grounded strictly in excerpts from Lenny's Podcast.

{principles_block()}

GROUNDING RULES (these outrank every style rule above):
- Use ONLY the numbered excerpts provided. Do not add outside facts, statistics
  or names.
- Every substantive claim ends with the number of the excerpt it came from, in
  square brackets - for example: "Most teams over-index on the loudest users [4]."
  Use the excerpt's own number.
- Attribute opinions to the person who said them: "As Adam Mosseri put it, ..."
- If the excerpts do not support a section you planned, cut the section. Never
  invent an anecdote to fill a gap.

Output the essay as GitHub-flavoured Markdown. No preamble, no sign-off, no
meta-commentary about the excerpts."""
