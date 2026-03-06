import os
import re
import sys
from pathlib import Path

import anthropic


DEFAULT_MODEL = "claude-sonnet-4-6"
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _load_env():
    """Load variables from .env file if it exists."""
    if not ENV_FILE.exists():
        return
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key and key not in os.environ:
                os.environ[key] = value

PROMPT_TEMPLATE = """You are a meeting notes assistant. Write notes in a compact, shorthand style — like someone jotting things down during the meeting. Dense and scannable, not polished prose.

The output has three sections in this exact order:

**Section 1 — Summary**
Start with "Summary" as a bold label, followed by 2-4 short bullet points capturing the most important outcomes. Focus on what was decided, what is happening next, and where things are heading. Keep it outcome-oriented, not a recap of the discussion.

**Section 2 — Action Items**
Bold label "Action Items", followed by concrete tasks that need to be done. Only include real action items that were explicitly discussed — if there are none, omit this section entirely. Do not invent action items. Keep to 2-3 items max.

**Section 3 — Discussion Notes**
Bold label "Discussion Notes", followed by the full detailed notes in the format described below.

Length rules for Discussion Notes:
- Scale detail inversely with meeting length. Short meetings (under 30 min) can have more granular coverage. Long meetings (1hr+) should compress aggressively — merge related sub-topics, drop repetitive back-and-forth, and keep only the substance.
- Prioritize decisions, outcomes, and new information over background discussion, context-setting, or rehashing known facts.
- When in doubt, cut. The Summary and Action Items already capture the essentials — Discussion Notes are for reference, not a transcript replacement.

Format rules for Discussion Notes:
- Structure as a nested bullet list. Top-level bullets are topic/section labels (short phrases, not sentences).
- Use 2-3 levels of nesting under each topic for details, context, decisions, and next steps.
- Pack multiple related pieces of info into a single bullet using commas or semicolons rather than splitting into separate sub-points.
- Write in fragments and shorthand, not full sentences. Drop unnecessary words.
- Bold **key decisions**, **important names/companies**, **blockers**, and **action items** inline.
- Preserve specific names, companies, numbers, timelines, and product names.
- Do not use headings (##), horizontal rules (---), or any other markdown structure — only bold labels for the three sections and nested bullet points.
- Do not invent information not in the transcript.
- Do not attribute statements or actions to specific people unless the transcript explicitly states "X will do Y" or "X said Y". Never guess who said something or who was being referred to based on context.
- Skip greetings, small talk, and meta-discussion about the meeting itself.
- Output ONLY the three sections, no preamble or explanation.

Transcript:
{transcript}"""


def _clean_for_notion(text):
    """Normalize markdown for clean Notion paste.

    - Converts space-based bullet indentation to tabs (Notion nests on tabs).
    - Removes blank lines between consecutive bullet items.
    """
    lines = text.split("\n")
    result = []

    # Detect indent unit from the first indented bullet
    indent_unit = 4
    for line in lines:
        m = re.match(r"^( +)- ", line)
        if m:
            indent_unit = len(m.group(1))
            break

    for i, line in enumerate(lines):
        # Convert space-indented bullets to tab-indented
        m = re.match(r"^( +)(- )", line)
        if m:
            spaces = len(m.group(1))
            level = max(1, round(spaces / indent_unit))
            line = "\t" * level + m.group(2) + line[m.end():]

        # Skip blank lines between consecutive bullet lines
        if line.strip() == "":
            # Look ahead: if next non-empty line is a bullet, skip this blank line
            next_is_bullet = False
            for j in range(i + 1, len(lines)):
                if lines[j].strip():
                    next_is_bullet = bool(re.match(r"^[\t ]*- ", lines[j]))
                    break
            # Look back: was the previous non-empty line a bullet?
            prev_is_bullet = False
            for j in range(len(result) - 1, -1, -1):
                if result[j].strip():
                    prev_is_bullet = bool(re.match(r"^[\t ]*- ", result[j]))
                    break
            if prev_is_bullet and next_is_bullet:
                continue

        result.append(line)

    return "\n".join(result)


def check_api_key():
    """Load .env and verify the Anthropic API key is set."""
    _load_env()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or api_key == "your-api-key-here":
        print("Error: ANTHROPIC_API_KEY not configured.", file=sys.stderr)
        print("Add your key to the .env file in the project root.", file=sys.stderr)
        return False
    return True


def generate_notes(transcript, meeting_name, date, model=None):
    """Generate structured meeting notes from a transcript using Claude API.

    Args:
        transcript: The meeting transcript text.
        meeting_name: Name of the meeting.
        date: Date string (YYYY-MM-DD).
        model: Claude model name (default: claude-sonnet-4-6).

    Returns:
        str: Formatted markdown notes.
    """
    if not check_api_key():
        return None

    model = model or DEFAULT_MODEL
    prompt = PROMPT_TEMPLATE.format(transcript=transcript)

    print(f"Generating notes with {model}...")

    client = anthropic.Anthropic()

    message = client.messages.create(
        model=model,
        max_tokens=16384,
        messages=[{"role": "user", "content": prompt}],
    )

    notes_body = message.content[0].text
    return _clean_for_notion(notes_body)
