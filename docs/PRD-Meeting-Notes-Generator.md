# Product Requirements Document: Local Meeting Notes Generator

**Version:** 1.0
**Date:** 2026-01-21
**Status:** Pending IT Security Approval

---

## Executive Summary

A privacy-first CLI tool that records meeting audio, transcribes it locally, and generates structured notes with minimal dependencies. Designed for organizations handling sensitive data where all information must remain on local machines.

---

## Overview

### Purpose
Enable employees to generate structured meeting notes and action items from Google Meets calls without using external services that process sensitive company data.

### Core User Flow
1. User runs single command before meeting starts: `meeting-notes start "Meeting Name"`
2. Tool records system audio throughout meeting
3. User stops recording: `Ctrl+C` or `meeting-notes stop`
4. Tool automatically:
   - Transcribes audio using local Whisper
   - Generates notes using LLM (local or API - pending IT approval)
   - Saves transcript + notes as markdown files
5. User gets notification when processing complete
6. User manually imports markdown file into Notion or other tools

---

## Data Locality & Security

**CRITICAL: This section details where data lives at every step of the process.**

### Data Flow & Storage Locations

#### 1. Audio Recording Phase
- **Data:** Raw meeting audio
- **Location:** Local file system only (`~/meeting-notes/[meeting-name]/audio.wav`)
- **Processing:** Captured via ffmpeg using macOS AVFoundation framework
- **Network Activity:** None
- **Retention:** Can be automatically deleted after successful transcription

#### 2. Transcription Phase
- **Data:** Audio file → Text transcript
- **Location:** Local file system only (`~/meeting-notes/[meeting-name]/transcript.txt`)
- **Processing:** Whisper AI model runs locally on user's MacBook
- **Model Storage:** Whisper model files stored in `~/.cache/whisper/` (downloaded once, ~1.5GB for medium model)
- **Network Activity:** None during transcription (model download only happens once during setup)
- **Third-party Access:** Zero - Whisper runs completely offline

#### 3. Note Generation Phase (Two Options)

**OPTION A: Local LLM (Maximum Privacy - Recommended for Sensitive Data)**
- **Data:** Transcript text → Structured notes
- **Location:** All processing happens in local RAM, output to `~/meeting-notes/[meeting-name]/notes.md`
- **Processing:** Ollama runs LLM model locally on MacBook
- **Model Storage:** LLM model stored in `~/.ollama/models/` (~4-40GB depending on model size)
- **Network Activity:** None during note generation (model download only happens once during setup)
- **Third-party Access:** Zero - complete air-gapped operation after initial model download
- **RAM Usage:** 8-16GB during processing (within M4 24GB capacity)

**OPTION B: API-based LLM (Faster, Requires External Service)**
- **Data:** Transcript text sent to external API via HTTPS
- **Location:**
  - Transcript temporarily transmitted to Anthropic/OpenAI servers
  - Response (notes) saved to `~/meeting-notes/[meeting-name]/notes.md`
- **Processing:** Claude API (Anthropic) or GPT API (OpenAI)
- **Network Activity:** HTTPS POST request with transcript, HTTPS response with notes
- **Third-party Access:**
  - Transcript content sent to API provider (Anthropic or OpenAI)
  - Encrypted in transit (TLS 1.3)
  - API providers claim no data retention for API calls (per their terms)
  - **⚠️ DATA LEAVES LOCAL MACHINE** - requires IT security approval
- **API Key Storage:** Stored in local config file `~/.meeting-notes/config.json`

#### 4. Storage Phase
- **Data:** Final transcript.txt and notes.md files
- **Location:** `~/meeting-notes/[date]_[meeting-name]/`
- **Format:** Plain text files (Markdown and TXT)
- **Encryption:** Standard macOS file system encryption (FileVault if enabled)
- **Backup:** Only via user's existing backup solution (Time Machine, etc.)
- **Network Activity:** None

### File Structure
```
~/meeting-notes/
├── 2026-01-21_project-sync/
│   ├── audio.wav          # Optional: can be auto-deleted
│   ├── transcript.txt     # Plain text transcript
│   └── notes.md          # Structured notes with action items
├── 2026-01-21_client-call/
│   ├── audio.wav
│   ├── transcript.txt
│   └── notes.md
└── .config.json          # Tool configuration (API keys if Option B)
```

### Summary: Data Locality by Option

| Step | Option A (Local LLM) | Option B (API LLM) |
|------|---------------------|-------------------|
| Audio Recording | ✅ 100% Local | ✅ 100% Local |
| Transcription | ✅ 100% Local | ✅ 100% Local |
| Note Generation | ✅ 100% Local | ❌ Sent to API Provider |
| Final Storage | ✅ 100% Local | ✅ 100% Local |
| **Overall** | **✅ Complete Air-Gap** | **⚠️ Requires External Service** |

---

## Technical Specification

### System Requirements
- **Hardware:** MacBook Pro M4 with 24GB RAM (or similar)
- **OS:** macOS 13.0+ (Ventura or later)
- **Python:** 3.9 or higher
- **Disk Space:**
  - Base installation: ~2GB
  - Option A (Local LLM): Additional 4-40GB for model
  - Per meeting: ~100-500MB (audio) + ~50KB (transcript) + ~5KB (notes)

### Technical Stack

**Core Dependencies (Required):**
- **Python 3.9+** - Main application language
- **ffmpeg** - Audio recording via AVFoundation framework
- **openai-whisper** - Local speech-to-text processing
- **PyYAML** - Configuration file management

**LLM Option A Dependencies (Local Processing):**
- **Ollama** - Local LLM runtime environment
- **Llama 3.1 (8B or 70B model)** - Language model for note generation

**LLM Option B Dependencies (API Processing):**
- **anthropic** or **openai** Python package - API client library
- **Active internet connection** - Required during note generation only
- **API Key** - Stored locally in config file

### Audio Recording Implementation

**Method:** ffmpeg with macOS AVFoundation

**Why this approach:**
- Zero additional dependencies (ffmpeg required for Whisper anyway)
- Fully automatable from CLI
- No virtual audio devices needed
- Single macOS permission grant (one-time)
- Direct audio capture without video overhead

**Technical Details:**
```bash
# List available audio devices (auto-detection)
ffmpeg -list_devices true -f avfoundation -i ""

# Record system audio
ffmpeg -f avfoundation -i ":1" -ar 16000 -ac 1 output.wav
```

**Permissions Required:**
- macOS Microphone Access (one-time permission prompt)
- No screen recording permission needed
- No accessibility permissions needed

---

## User Interface

### Commands
```bash
# Start recording a meeting
meeting-notes start "Meeting Name"

# Stop recording and begin processing
meeting-notes stop
# or Ctrl+C

# List past meetings (optional)
meeting-notes list

# Configure LLM option
meeting-notes config --llm local    # Option A
meeting-notes config --llm api      # Option B
```

### Output Format (notes.md)
```markdown
# Meeting: [Name]
Date: [YYYY-MM-DD]
Duration: [MM:SS]

## Summary
[2-3 paragraph overview of key discussion points and decisions]

## Key Points
- [Main topic 1 with supporting arguments]
- [Main topic 2 with supporting arguments]
- [Main topic 3 with supporting arguments]
- [Additional points as needed]

## Action Items
- [ ] [Action item with owner if mentioned]
- [ ] [Action item with deadline if mentioned]
- [ ] [Action item with context]
```

---

## Processing Time Estimates

**Hardware:** MacBook Pro M4 with 24GB RAM

### Option A: Local LLM
- Audio Recording: Real-time (meeting duration)
- Whisper Transcription: ~5-10 minutes for 60min meeting
- Note Generation (Local): ~3-5 minutes (8B model) or ~8-12 minutes (70B model)
- **Total Processing Time:** 8-22 minutes after meeting ends

### Option B: API LLM
- Audio Recording: Real-time (meeting duration)
- Whisper Transcription: ~5-10 minutes for 60min meeting
- Note Generation (API): ~30-60 seconds
- **Total Processing Time:** 6-11 minutes after meeting ends

---

## Security Considerations

### Option A (Local LLM) - Maximum Security
✅ **All data remains on local machine at all times**
✅ No network activity during recording, transcription, or note generation
✅ No third-party data access
✅ No API keys or credentials required
✅ Can operate completely offline after initial setup
✅ No telemetry or analytics
✅ Audio files can be auto-deleted after processing

**Potential Concerns:**
- Large model downloads during initial setup (~4-40GB)
- Models downloaded from Ollama/HuggingFace (one-time, can be verified)

### Option B (API LLM) - Convenience vs. Privacy Trade-off
✅ Audio recording and transcription remain 100% local
✅ Only processed transcript text sent to API (not raw audio)
✅ Encrypted transmission (TLS 1.3)
✅ No audio stored by API provider

⚠️ **Security Trade-offs:**
- Meeting transcript content sent to third-party (Anthropic or OpenAI)
- Requires trust in API provider's data handling policies
- Requires internet connection during note generation
- API keys stored in local config file
- Compliance with API provider's terms of service

**API Provider Data Policies (as of 2026):**
- Anthropic (Claude): Claims zero data retention for API calls
- OpenAI: Claims no training on API data (enterprise tier)
- Both providers: Data encrypted in transit and at rest

### Recommended Security Posture by Data Sensitivity

| Data Sensitivity | Recommended Option |
|-----------------|-------------------|
| Public/Low | Either option acceptable |
| Internal/Medium | Option A preferred, Option B acceptable with approval |
| Confidential/High | Option A only |
| Regulated (HIPAA, etc.) | Option A only + additional controls |

---

## Implementation Phases

### Phase 1: Core Functionality (MVP)
- CLI tool with start/stop commands
- Audio recording via ffmpeg
- Local Whisper transcription
- Option A: Local LLM note generation (Ollama)
- Basic markdown output
- **Estimated Development Time:** Not specified per guidelines

### Phase 2: Enhanced Features (Optional)
- Option B: API LLM support
- Automatic audio file cleanup
- Meeting list/history command
- Configuration management
- Error handling and recovery

### Phase 3: Quality of Life (Future)
- Custom note templates
- Multiple output formats
- Basic speaker detection (if Whisper supports it)
- Improved error messages

---

## Out of Scope (v1)

❌ GUI interface
❌ Real-time transcription
❌ Metadata tracking (attendees, tags, projects)
❌ Search functionality
❌ Direct Notion API integration
❌ Speaker diarization (identifying who said what)
❌ Multi-language support (English only initially)
❌ Cloud storage or backup
❌ Mobile support
❌ Calendar integration
❌ Automatic meeting detection

---

## Cost Analysis

### Option A (Local LLM)
- **Setup Cost:** $0
- **Per-Meeting Cost:** $0
- **Infrastructure:** Uses existing MacBook hardware
- **Ongoing Costs:** Electricity only (negligible)

### Option B (API LLM)
- **Setup Cost:** $0
- **Per-Meeting Cost:** ~$0.10-$0.50 per meeting (depending on transcript length)
- **Monthly Cost Estimate:** ~$5-20 for 50 meetings/month
- **Requires:** API account with Anthropic or OpenAI

---

## IT Security Approval Checklist

Please review and approve the following:

### Required Approvals
- [ ] **Option A (Local LLM):** Approved for use with sensitive data
- [ ] **Option B (API LLM):** Approved for use with [specify data classification levels]

### Infrastructure Approvals
- [ ] Python 3.9+ installation permitted
- [ ] ffmpeg installation permitted
- [ ] openai-whisper package installation permitted
- [ ] Ollama installation permitted (Option A)
- [ ] Anthropic/OpenAI API access permitted (Option B)

### Network & Security
- [ ] Model downloads from HuggingFace/Ollama permitted (one-time, ~4-40GB)
- [ ] macOS microphone permission acceptable
- [ ] Local file storage in `~/meeting-notes/` acceptable
- [ ] API key storage in local config file acceptable (Option B)

### Compliance Requirements
- [ ] Meets data residency requirements
- [ ] Meets data retention/deletion policies
- [ ] Meets third-party vendor assessment requirements (Option B)
- [ ] No additional compliance controls required

### Questions for IT Security
1. Are encrypted API calls to Anthropic/OpenAI acceptable for meeting transcripts?
2. What data classification levels can be processed with Option B?
3. Are there any restrictions on installing Python packages or Ollama?
4. Should audio files be automatically deleted after successful transcription?
5. Are there any required audit logging or access controls?
6. Should the tool integrate with existing DLP or security monitoring?

---

## Success Metrics

- Tool successfully records and processes 95%+ of meetings without errors
- Processing completes within expected timeframes
- Generated notes contain accurate summaries and action items
- Zero data leakage incidents
- User adoption rate among target teams

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Audio capture fails | High | Fallback to manual recording, error alerts |
| Whisper transcription errors | Medium | Review transcript before note generation |
| LLM generates inaccurate notes | Medium | User reviews all notes before use |
| Disk space exhaustion | Low | Auto-delete audio files after processing |
| API key leakage (Option B) | High | Secure config file permissions, no git commits |
| Meeting contains PII/sensitive data | High | Clear usage guidelines, Option A for sensitive meetings |

---

## Appendix: Technical Deep Dive

### Why Whisper for Transcription?
- Industry-leading accuracy for speech-to-text
- Runs completely offline
- Open source (MIT license)
- Optimized for Apple Silicon (M4)
- Supports multiple model sizes for speed/accuracy trade-offs
- Active development and community support

### Why Ollama for Local LLM? (Option A)
- Simplest local LLM deployment method
- Native Apple Silicon support
- Automatic model management
- Low overhead, high performance
- Growing model library (Llama, Mistral, etc.)
- Easy to upgrade models as they improve

### Why Claude API? (Option B)
- Best-in-class output quality for summarization tasks
- Structured output support
- Fast processing times
- Competitive pricing
- Strong data privacy commitments
- Excellent API documentation

---

## Approval & Sign-off

**Prepared by:** [Your Name]
**Date:** 2026-01-21

**IT Security Review:**
- Reviewed by: ________________
- Date: ________________
- Status: ☐ Approved (Option A) ☐ Approved (Option B) ☐ Rejected ☐ Needs Revision

**Comments:**
_________________________________
_________________________________
_________________________________

