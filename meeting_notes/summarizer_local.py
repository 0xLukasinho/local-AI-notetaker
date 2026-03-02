import ollama


PROMPT_TEMPLATE = """You are a meeting notes assistant. Your job is to produce COMPREHENSIVE, EXHAUSTIVE notes that capture EVERY topic discussed in the meeting. Do not skip or merge topics. Do not summarize — document.

CRITICAL RULES:
- Cover EVERY topic and discussion point in the transcript. If something was discussed for more than 30 seconds, it MUST appear in the notes.
- Organize notes by topic or agenda item. Each major topic is a top-level bullet point.
- Under each topic, use nested sub-points (2-3 levels deep) to capture details, arguments, context, and nuance.
- Preserve specific details: names of people, companies, products, numbers, timelines, and direct quotes.
- Capture the reasoning and arguments behind points, not just the conclusions.
- Note sentiments, concerns, disagreements, and open questions that were raised.
- Bold **key takeaways**, **important decisions**, and **critical terms**.
- Do NOT write a separate summary. The structured bullet points ARE the notes.
- Do NOT merge distinct discussion topics into one generic theme. Keep them separate.
- Do NOT invent information that is not in the transcript.
- The notes should be LONG and DETAILED. Err on the side of including too much rather than too little.
- At the end, list all action items with owners and deadlines if mentioned.
- Output ONLY the markdown content below, no preamble or explanation.

Transcript:
{transcript}

Generate the notes in this exact format:

## Key Points
- [Topic 1]
    - [Detail or argument with context]
        - [Supporting detail, specific names, numbers, or quotes]
        - [Additional nuance or sub-point]
    - [Another aspect of this topic]
        - [Details]
- [Topic 2]
    - [Detail with context]
        - [Sub-detail]
        - [Sub-detail]
    - [Another aspect]
        - [Details]
- [Topic 3]
    - [Continue for EVERY topic discussed]
- [Continue until ALL discussion points are covered]

## Action Items
- [ ] [Action item — owner — deadline if mentioned]
- [ ] [Additional items as needed]"""


def check_ollama(model):
    """Verify Ollama is running and the model is available."""
    try:
        models = ollama.list()
        available = [m.model for m in models.models]
        # Check with and without tag suffix
        if not any(model in name or name.startswith(model) for name in available):
            print(f"Model '{model}' not found. Available models: {', '.join(available)}")
            print(f"Pull it with: ollama pull {model}")
            return False
    except Exception as e:
        print(f"Error connecting to Ollama: {e}")
        print("Make sure Ollama is running: ollama serve")
        return False
    return True


def generate_notes(transcript, meeting_name, date, model="qwen3:32b"):
    """Generate structured meeting notes from a transcript using Ollama.

    Args:
        transcript: The meeting transcript text.
        meeting_name: Name of the meeting.
        date: Date string (YYYY-MM-DD).
        model: Ollama model name.

    Returns:
        str: Formatted markdown notes.
    """
    if not check_ollama(model):
        return None

    prompt = PROMPT_TEMPLATE.format(transcript=transcript)

    print(f"Generating notes with {model} (this may take a few minutes)...")

    response = ollama.chat(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )

    notes_body = response.message.content

    # Build the full markdown document
    header = f"# Meeting: {meeting_name}\nDate: {date}\n"
    return f"{header}\n{notes_body}"
