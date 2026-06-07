import re
import shutil
import subprocess
import sys


DEFAULT_MODEL = "claude-opus-4-7"
CLAUDE_TIMEOUT_SECONDS = 600


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

    indent_unit = 4
    for line in lines:
        m = re.match(r"^( +)- ", line)
        if m:
            indent_unit = len(m.group(1))
            break

    for i, line in enumerate(lines):
        m = re.match(r"^( +)(- )", line)
        if m:
            spaces = len(m.group(1))
            level = max(1, round(spaces / indent_unit))
            line = "\t" * level + m.group(2) + line[m.end():]

        if line.strip() == "":
            next_is_bullet = False
            for j in range(i + 1, len(lines)):
                if lines[j].strip():
                    next_is_bullet = bool(re.match(r"^[\t ]*- ", lines[j]))
                    break
            prev_is_bullet = False
            for j in range(len(result) - 1, -1, -1):
                if result[j].strip():
                    prev_is_bullet = bool(re.match(r"^[\t ]*- ", result[j]))
                    break
            if prev_is_bullet and next_is_bullet:
                continue

        result.append(line)

    return "\n".join(result)


def _resolve_claude_cli():
    """Return the absolute path of the `claude` executable, or None.

    Uses shutil.which so we get the .cmd/.exe shim on Windows and a plain
    binary on macOS — and so subprocess can launch it without shell=True.
    """
    return shutil.which("claude")


def generate_notes(transcript, meeting_name, date, model=None):
    """Generate structured meeting notes by invoking the Claude Code CLI.

    Uses the user's Claude subscription (OAuth via Claude Code), not the
    Anthropic API. Sends the prompt over stdin to avoid Windows command-line
    length limits on long transcripts.

    Returns the cleaned markdown notes, or None on any failure.
    """
    claude_path = _resolve_claude_cli()
    if claude_path is None:
        print("Error: Claude Code CLI ('claude') not found on PATH.", file=sys.stderr)
        print(
            "Install Claude Code from https://claude.com/code and run "
            "'claude login' before using this tool.",
            file=sys.stderr,
        )
        return None

    model = model or DEFAULT_MODEL
    prompt = PROMPT_TEMPLATE.format(transcript=transcript)

    print(f"Generating notes with {model} via Claude Code (subscription)...")

    try:
        result = subprocess.run(
            [claude_path, "-p", "--model", model],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=CLAUDE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(
            f"Error: Claude Code did not respond within {CLAUDE_TIMEOUT_SECONDS}s.",
            file=sys.stderr,
        )
        return None

    if result.returncode != 0:
        print(f"Error: Claude Code exited with code {result.returncode}.", file=sys.stderr)
        if result.stderr:
            print(result.stderr.strip(), file=sys.stderr)
        return None

    notes_body = (result.stdout or "").strip()
    if not notes_body:
        print("Error: Claude Code returned empty output.", file=sys.stderr)
        return None

    return _clean_for_notion(notes_body)
