# Pretty Good AI — Voice Bot Build Plan

## Overview

Build a Python voice bot that calls `+1-805-439-8008`, simulates realistic patient scenarios, records and transcribes conversations, and surfaces bugs in Pretty Good AI's Athena agent.

---

## Stack

| Layer | Tool | Why |
|---|---|---|
| Outbound calling | **Twilio** | Programmable voice, call recording, media streams |
| Speech-to-text | **Deepgram** (streaming) | Low-latency, real-time transcription via WebSocket |
| LLM (patient brain) | **Claude claude-sonnet-4-6** | Drives realistic patient persona, steers conversation toward test scenarios |
| Text-to-speech | **ElevenLabs** or **Twilio TTS** | Natural-sounding voice output |
| Web server | **FastAPI + ngrok** | Receives Twilio webhooks locally during dev |
| Storage | Local filesystem (JSON + audio files) | Transcripts + call recordings (ogg/mp3) |

> Estimated API cost: ~$10–15 for 10+ calls across Twilio, Deepgram, Claude, and ElevenLabs.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  Orchestrator (Python)               │
│  - Loads test scenario (persona + goal)             │
│  - Initiates outbound call via Twilio REST API      │
└────────────────────┬────────────────────────────────┘
                     │ call initiated
                     ▼
┌─────────────────────────────────────────────────────┐
│               Twilio (PSTN bridge)                  │
│  - Connects to +1-805-439-8008                      │
│  - Streams audio both ways via WebSocket            │
│  - Records full call as ogg/mp3                     │
└────────┬─────────────────────────┬──────────────────┘
         │ agent audio (inbound)   │ bot audio (outbound)
         ▼                         ▲
┌─────────────────┐     ┌──────────────────────────────┐
│   Deepgram STT  │     │  ElevenLabs TTS              │
│  (streaming WS) │     │  (synthesizes bot speech)    │
└────────┬────────┘     └──────────────┬───────────────┘
         │ transcript text             │ audio bytes
         ▼                             │
┌─────────────────────────────────────┴──────────────┐
│              Claude (Patient Brain)                 │
│  - Given: persona, scenario goal, conversation so far│
│  - Returns: next patient utterance                  │
│  - Knows when to end the call naturally             │
└─────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────┐
│              Logger / Analyzer                      │
│  - Saves full transcript (JSON + plain text)        │
│  - Saves recording (ogg/mp3)                        │
│  - Post-call: Claude reviews transcript for bugs    │
└─────────────────────────────────────────────────────┘
```

---

## Project Structure

```
pgai-voice-bot/
├── main.py                  # Orchestrator: run scenarios, manage call lifecycle
├── bot_brain.py             # Claude-powered patient persona logic
├── telephony.py             # Twilio call setup, WebSocket media stream handler
├── stt.py                   # Deepgram streaming STT
├── tts.py                   # ElevenLabs (or Twilio) TTS
├── analyzer.py              # Post-call bug detection via Claude
├── scenarios/
│   ├── base.py              # Scenario dataclass
│   ├── scheduling.py        # Simple appointment scheduling
│   ├── reschedule.py        # Rescheduling / canceling
│   ├── refill.py            # Medication refill
│   ├── office_info.py       # Hours, location, insurance questions
│   └── edge_cases.py        # Interruptions, ambiguous requests, unusual scenarios
├── recordings/              # Auto-created; stores .ogg/.mp3 per call
├── transcripts/             # Auto-created; stores .json + .txt per call
├── bug_report.md            # Running bug log (auto-appended by analyzer)
├── .env.example             # Required env vars (no secrets committed)
├── requirements.txt
└── README.md
```

---

## Call Flow (per call)

1. **Select scenario** — pick from scenario list (or rotate through all)
2. **Spin up FastAPI server** + ngrok tunnel (or use a deployed server)
3. **Initiate call** via Twilio REST: `POST /2010-04-01/Accounts/{SID}/Calls`
   - TwiML points Twilio to our WebSocket endpoint
4. **Twilio connects** to `+1-805-439-8008` and opens bidirectional audio stream
5. **Deepgram STT** transcribes agent audio in real time → text chunks arrive via WS
6. **Claude (patient brain)** receives:
   - System prompt: persona + scenario goal + rules (be realistic, don't rush, steer naturally)
   - Full conversation history so far
   - Returns next patient utterance
7. **ElevenLabs TTS** converts Claude's text → audio → streamed back to Twilio
8. Repeat until scenario concludes or call times out (max ~4 min)
9. **Post-call**: Twilio delivers recording URL → download as ogg/mp3
10. **Analyzer**: Claude reviews full transcript, flags potential bugs → appends to `bug_report.md`

---

## Patient Personas & Scenarios (10 required calls)

| # | Persona | Scenario | Edge Case? |
|---|---|---|---|
| 1 | Maria, 52 | Schedule new appointment (primary care) | No |
| 2 | James, 34 | Reschedule existing appointment | No |
| 3 | Linda, 68 | Cancel appointment, reason: transportation | No |
| 4 | Carlos, 45 | Medication refill request (lisinopril) | No |
| 5 | Sarah, 29 | Ask about office hours + insurance accepted | No |
| 6 | Dave, 61 | Request appointment on a weekend (Sunday) | Yes — should be rejected |
| 7 | Priya, 38 | Starts scheduling, then changes mind mid-sentence, restarts | Yes — interruption/barge-in |
| 8 | Tom, 55 | Asks for a refill for a controlled substance (Adderall) | Yes — policy edge |
| 9 | Nancy, 77 | Very slow speech, long pauses, asks agent to repeat | Yes — pacing/latency |
| 10 | Anonymous | Speaks unclearly, gives conflicting info, changes request multiple times | Yes — ambiguity |
| 11+ | Various | Additional creative edge cases (urgency, billing, new patient flow) | Yes |

---

## Claude System Prompt Template (Patient Brain)

```
You are roleplaying as a patient calling a medical office's AI phone agent.

Persona: {persona_name}, {persona_age} years old. {persona_background}

Your goal for this call: {scenario_goal}

Rules:
- Speak naturally, like a real person on the phone. Use "um", "uh", short sentences.
- Don't volunteer all info at once — respond to what the agent says.
- Stay in character. Don't break the fourth wall.
- If the agent makes an error or says something wrong, note it in your next turn naturally (e.g., "Wait, I thought you said...")
- End the call naturally when your goal is complete or clearly unachievable.
- Never mention that you are a bot or AI.

Conversation so far:
{conversation_history}

Agent just said: "{last_agent_utterance}"

Respond as {persona_name}:
```

---

## Post-Call Bug Analyzer Prompt

```
You are a QA engineer reviewing a transcript from a medical office AI phone agent.

Transcript:
{full_transcript}

Identify any bugs, quality issues, or unexpected behaviors. For each:
- What happened
- Why it's a problem
- Severity: Low / Medium / High
- Timestamp or quote from transcript

Be specific. Focus on real issues: wrong info, policy violations, poor handling of edge cases, broken flows.
```

---

## Environment Variables (`.env.example`)

```
# Twilio
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_PHONE_NUMBER=       # Your Twilio number (E.164)

# Deepgram
DEEPGRAM_API_KEY=

# ElevenLabs
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID=

# Anthropic
ANTHROPIC_API_KEY=

# Ngrok (for local dev)
NGROK_AUTH_TOKEN=

# Target number (do not change)
TARGET_NUMBER=+18054398008
```

---

## Run Instructions (target: single command)

```bash
# 1. Clone and install
git clone https://github.com/your-username/pgai-voice-bot
cd pgai-voice-bot
pip install -r requirements.txt
cp .env.example .env   # fill in your keys

# 2. Run all scenarios
python main.py --run-all

# Or run a single scenario
python main.py --scenario scheduling
```

---

## Key Design Choices

**Why Twilio for telephony?** It has the best-documented programmable voice + media streaming API and supports outbound PSTN calls with recording out of the box. Twilio's `<Stream>` TwiML lets us tap bidirectional audio without post-processing.

**Why Deepgram over Whisper?** Deepgram's streaming API returns partial transcripts in ~300ms vs. Whisper's batch latency (~2–5s). Real-time STT is the bottleneck for natural turn-taking; Deepgram solves it.

**Why Claude as the patient brain?** The challenge requires realistic, scenario-steered conversation, not scripted outputs. Claude handles mid-call pivots, interrupted sentences, and natural language variation without rigid state machines.

**Why ElevenLabs for TTS?** The call will be rejected if voice quality is poor. ElevenLabs produces the most natural-sounding output with streaming support; Twilio's built-in TTS is robotic by comparison and will hurt the evaluation.

**Turn-taking strategy:** Deepgram fires an `is_final=true` event when a speaker stops. We use a 700ms silence buffer after `is_final` before sending bot response, preventing barge-in artifacts while keeping latency low.

---

## Bug Report (Running)

See `bug_report.md` — auto-populated after each call by the analyzer.

**Format per entry:**
```
## Bug [N]
Severity: High / Medium / Low
Call: transcript-XX.json @ ~[timestamp]
What happened: [description]
Why it's a problem: [impact]
Quote: "[relevant agent utterance]"
```

---

## Iteration Plan

After calls 1–3: listen to recordings, check turn-taking, adjust silence buffer and TTS speed if needed.

After calls 4–6: review transcripts for scenario steering quality; tighten Claude system prompt if bot is going off-script.

After calls 7–10: focus on edge case scenarios; verify bug analyzer is catching real issues.

Final pass: clean code, write README, record Loom.
