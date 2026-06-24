# PGAI Voice Bot — Patient Simulator

A Python voice bot that places real phone calls to Pretty Good AI's test agent
at **+1‑805‑439‑8008**, plays the role of a patient (scheduling, refills,
edge cases), records and transcribes every call, and runs an LLM-based bug
analyzer over each transcript.

Built for the *Pretty Good AI – AI Engineering Challenge*.

---

## Architecture

```
                ┌──────────────────────────┐
                │  main.py  (CLI)          │
                │  – picks scenario        │
                │  – calls Twilio REST     │
                │  – polls call status     │
                │  – runs analyzer         │
                └────────────┬─────────────┘
                             │  POST /Calls (record=true, url=/twiml)
                             ▼
┌──────────────────────────────────────────────────────────────┐
│                       Twilio Cloud                           │
│  – dials +1-805-439-8008                                     │
│  – records the call (mp3, dual channel)                      │
│  – streams call audio over WebSocket to our server           │
└────────────────────────────┬─────────────────────────────────┘
                             │  wss://<ngrok>/ws/twilio
                             ▼
┌──────────────────────────────────────────────────────────────┐
│        server.py (FastAPI)  +  call_session.py               │
│                                                              │
│   inbound μ-law 8 kHz ──▶ Deepgram STT (nova-2-phonecall)    │
│                                       │ utterance text       │
│                                       ▼                      │
│                            BotBrain (Claude sonnet-4-6)      │
│                            – persona + goal + history        │
│                            – returns next patient line       │
│                                       │ text                 │
│                                       ▼                      │
│                            ElevenLabs TTS (ulaw_8000)        │
│                                       │ μ-law bytes          │
│                                       ▼                      │
│                       framed + base64 → Twilio WS            │
└──────────────────────────────────────────────────────────────┘
                             │
                             ▼
                  transcripts/<call_id>.{json,txt}
                  recordings/<call_id>.mp3
                  bug_report.md  (appended by analyzer.py)
```

**Key design choices**

- **Hand-rolled pipeline (no Pipecat / Realtime API).** Gives the reviewer
  visible code at every layer; lets us swap any single stage (STT, brain, TTS)
  without touching the others.
- **Deepgram `nova-2-phonecall` + `utterance_end_ms=1000`.** Phone-tuned model
  for 8 kHz μ-law; the 1 s utterance-end window is the silence buffer that
  triggers a patient turn — simpler and more reliable than a VAD layered on top.
- **Claude Sonnet 4.6 as the patient brain, with an `[END_CALL]` sentinel.**
  Sonnet handles mid-call pivots, ambiguity, and natural pacing far better than
  Haiku; the sentinel lets the brain itself decide when to hang up rather than
  relying on a hard turn cap.
- **ElevenLabs Flash v2.5 streamed at `ulaw_8000`.** Zero resampling between
  TTS and Twilio. Flash is the lowest-latency model — first audio in
  ~300–500 ms, which is what keeps the conversation lucid.
- **One outbound number** (as the challenge requires) — your purchased Twilio
  number is used for every call.

---

## Setup

### 1. Accounts (one-time)

| Service | What you need | Free-tier? |
|---|---|---|
| Twilio | Account SID, auth token, one purchased US local number | Trial won't work — must upgrade ($20) |
| Deepgram | API key | $200 free credit, no card |
| ElevenLabs | API key | Free tier OK for ~10 calls |
| Anthropic | API key with messages access | Paid |
| ngrok | Auth token | Free |

### 2. Install

Requires **Python 3.10+** (we use PEP 604 union types). On macOS:
`brew install python@3.12`.

```bash
git clone <this-repo>
cd PrettyGoodAiChallenge
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# now open .env and fill in every key
```

You also need the `ngrok` CLI installed and authed:

```bash
brew install ngrok
ngrok config add-authtoken <your-token>
```

### 3. Run

The easy path — single script, foregrounds the server, prints the tunnel URL:

```bash
./run.sh                       # start server + tunnel, leave them running
# then in another terminal:
export PUBLIC_BASE_URL=<the URL run.sh printed>
python main.py list            # see all scenarios
python main.py call schedule-new
python main.py run-all         # all 10+ calls back to back
```

Or do all three steps in one shot:

```bash
./run.sh run-all
```

### 4. Outputs

After each call:

- `recordings/<call_id>.mp3` — full dual-channel audio of the call
- `transcripts/<call_id>.json` — timestamped turns + metadata
- `transcripts/<call_id>.txt`  — human-readable transcript
- `bug_report.md`              — Claude-reviewed issues, appended per call

---

## CLI reference

```bash
python main.py list                  # show scenarios
python main.py call <slug>           # place one call
python main.py run-all               # run every scenario
python main.py analyze <call_id>     # re-run the analyzer over a saved transcript
```

Flags:
- `--max-wait <sec>`  how long to poll Twilio before giving up (default 300)
- `--no-analyze`      skip the post-call analyzer
- `--gap <sec>`       seconds between calls in `run-all` (default 10)

---

## Project layout

```
PrettyGoodAiChallenge/
├── server.py              # FastAPI: /twiml, /ws/twilio, /call-status, /recording-status
├── call_session.py        # per-call orchestration (STT -> brain -> TTS)
├── bot_brain.py           # Claude patient brain
├── stt.py                 # Deepgram streaming wrapper
├── tts.py                 # ElevenLabs streaming wrapper (ulaw_8000)
├── telephony.py           # Twilio call placement + recording fetch
├── analyzer.py            # post-call bug analyzer (Claude)
├── main.py                # CLI orchestrator
├── run.sh                 # one-command: server + ngrok + (optional) CLI cmd
├── scenarios/__init__.py  # 12 patient personas (10 required + 2 extras)
├── recordings/            # mp3s, populated per call
├── transcripts/           # json + txt per call
├── bug_report.md          # appended after each call
├── .env.example
└── requirements.txt
```

---

## Cost (10 calls × ~2 min each, est.)

| Item | Cost |
|---|---|
| Twilio (outbound + recording) | ~$0.50 |
| Twilio number rental | ~$1.15 / mo |
| Deepgram (nova-2) | ~$0.10 |
| ElevenLabs (Flash) | ~$1–2 |
| Claude (Sonnet 4.6, in+out) | ~$0.50 |
| **Total per 10-call run** | **~$3–5** |

Well under the $20 budget in the brief.

---

## Iteration notes

Calls 1–3: turn-taking validation. If the bot stomps on the agent, raise
`utterance_end_ms` in `stt.py`. If pauses feel too long, lower it.

Calls 4–6: review transcripts for steering quality. If the brain wanders off
goal, tighten the persona's `goal` text in `scenarios/__init__.py`.

Calls 7–10: hammer edge cases (`weekend-edge`, `controlled-refill`,
`urgent-symptoms`, `midcall-pivot`, `ambiguous-caller`, `slow-elderly`). These
are where the interesting bugs surface.
