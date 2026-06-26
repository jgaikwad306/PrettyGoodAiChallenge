"""Vapi-based call orchestration.

Replaces the prior Twilio + Deepgram + Anthropic + ElevenLabs stack with a
single managed pipeline. We build a transient assistant per call (so the
persona/system prompt can vary per scenario), POST an outbound call to Vapi,
poll until the call ends, then pull the transcript and recording.

Vapi handles: telephony (registered numbers — no STIR/SHAKEN spam blocking),
streaming STT (Deepgram), LLM orchestration (Anthropic), streaming TTS
(ElevenLabs), and turn-taking. Provider credentials are configured once in
the Vapi dashboard; we do not pass API keys per-call.

Output shape matches the prior pipeline:
- transcripts/<call_id>.json — `{call_id, scenario, persona, goal, turns: [...]}`
- transcripts/<call_id>.txt  — human-readable transcript
- recordings/<call_id>.mp3   — downloaded from Vapi's signed URL
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import pathlib
import time

import httpx

from scenarios.base import Scenario


log = logging.getLogger(__name__)

VAPI_BASE = "https://api.vapi.ai"
REPO = pathlib.Path(__file__).parent
TRANSCRIPTS_DIR = REPO / "transcripts"
RECORDINGS_DIR = REPO / "recordings"
TRANSCRIPTS_DIR.mkdir(exist_ok=True)
RECORDINGS_DIR.mkdir(exist_ok=True)

MODEL = "claude-haiku-4-5-20251001"
VOICE_MODEL = "eleven_flash_v2_5"
STT_MODEL = "nova-2-phonecall"

TERMINAL_STATUSES = {"ended", "failed"}

END_CALL_PHRASES = ["goodbye", "bye", "have a good day", "take care"]


SYSTEM_TEMPLATE = """You are roleplaying as a patient calling a medical office's AI phone agent.

Persona: {persona_name}, {persona_age} years old.
{persona_background}

Your goal for this call: {goal}

You called them — open the conversation. Your very first turn should be a short, natural greeting that hints at why you're calling (e.g. "Hi, um, I'm calling to schedule an appointment"). Then wait for the agent to respond.

How to talk (this is non-negotiable):
- Speak naturally, like a real person on the phone. Short sentences. Occasional "um", "uh", "let me think".
- One or two sentences per turn unless the agent asked something complex.
- Don't dump all your info at once — answer what was asked, wait for the next question.
- Stay in character. Don't break the fourth wall. Never say you are a bot, AI, or test.
- If the agent says something wrong, note it naturally on your next turn (e.g. "wait, I thought you said Tuesday?").
- If the agent gives the info you needed or completes your task, wrap up politely ("okay great, thanks, goodbye!") so the call ends.
- If the call is clearly stuck or the agent keeps misunderstanding after 2-3 retries, politely end the call by saying goodbye.
{extra_rules}

Output format:
- Respond with ONLY what the patient says out loud. No stage directions, no narration, no quotes.
"""


def _build_system_prompt(scenario: Scenario) -> str:
    extra = ""
    if scenario.extra_rules:
        bullets = "\n".join(f"- {r}" for r in scenario.extra_rules)
        extra = f"\nExtra rules for this persona:\n{bullets}"
    return SYSTEM_TEMPLATE.format(
        persona_name=scenario.persona_name,
        persona_age=scenario.persona_age,
        persona_background=scenario.persona_background,
        goal=scenario.goal,
        extra_rules=extra,
    )


def _assistant_config(scenario: Scenario) -> dict:
    voice_id = scenario.voice_id or os.environ.get("ELEVENLABS_VOICE_ID")
    if not voice_id:
        raise RuntimeError("No voice id (scenario.voice_id or ELEVENLABS_VOICE_ID)")
    return {
        "name": f"pgai-patient-{scenario.slug}",
        # Let the LLM generate a persona-appropriate opening line instead of
        # a hardcoded greeting — each scenario needs its own natural opener.
        "firstMessageMode": "assistant-speaks-first-with-model-generated-message",
        "model": {
            "provider": "anthropic",
            "model": MODEL,
            "temperature": 0.7,
            "maxTokens": 200,
            "messages": [{"role": "system", "content": _build_system_prompt(scenario)}],
        },
        "voice": {
            "provider": "11labs",
            "voiceId": voice_id,
            "model": VOICE_MODEL,
            "optimizeStreamingLatency": 3,
        },
        "transcriber": {
            "provider": "deepgram",
            "model": STT_MODEL,
            "language": "en-US",
        },
        "silenceTimeoutSeconds": 30,
        "maxDurationSeconds": 300,
        "endCallPhrases": END_CALL_PHRASES,
        "endCallFunctionEnabled": True,
        "recordingEnabled": True,
        "backgroundSound": "off",
    }


def _auth_headers() -> dict:
    key = os.environ.get("VAPI_API_KEY")
    if not key:
        raise RuntimeError("VAPI_API_KEY not set")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


async def place_call(scenario: Scenario, *, target_number: str) -> str:
    """Kick off an outbound Vapi call. Returns the Vapi call id."""
    phone_number_id = os.environ.get("VAPI_PHONE_NUMBER_ID")
    if not phone_number_id:
        raise RuntimeError("VAPI_PHONE_NUMBER_ID not set (provision a number in the Vapi dashboard)")
    body = {
        "phoneNumberId": phone_number_id,
        "customer": {"number": target_number},
        "assistant": _assistant_config(scenario),
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{VAPI_BASE}/call", headers=_auth_headers(), json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"Vapi /call failed {r.status_code}: {r.text}")
        data = r.json()
    call_id = data["id"]
    log.info("Vapi call started id=%s -> %s", call_id, target_number)
    return call_id


async def fetch_call(call_id: str) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{VAPI_BASE}/call/{call_id}", headers=_auth_headers())
        r.raise_for_status()
        return r.json()


async def wait_for_call(call_id: str, *, max_wait: int = 360, poll_every: int = 5) -> dict:
    """Poll until the call reaches a terminal status. Returns final call object."""
    start = time.time()
    last_status = ""
    while time.time() - start < max_wait:
        call = await fetch_call(call_id)
        status = call.get("status", "")
        if status != last_status:
            log.info("[%s] status=%s", call_id, status)
            last_status = status
        if status in TERMINAL_STATUSES:
            return call
        await asyncio.sleep(poll_every)
    log.warning("[%s] wait_for_call timed out after %ds", call_id, max_wait)
    return await fetch_call(call_id)


def _convert_messages(call: dict) -> list[dict]:
    """Map Vapi's `messages` list to our `{t, speaker, text}` shape.

    Vapi message roles we care about:
    - "bot"  -> our patient (the assistant we configured)
    - "user" -> the PGAI agent on the other end of the line
    System and tool messages are skipped.
    """
    raw = call.get("messages") or (call.get("artifact") or {}).get("messages") or []
    started_at_ms = None
    for m in raw:
        t_ms = m.get("time")
        if t_ms:
            started_at_ms = t_ms
            break

    turns: list[dict] = []
    for m in raw:
        role = m.get("role")
        if role not in ("bot", "user"):
            continue
        text = (m.get("message") or "").strip()
        if not text:
            continue
        t_ms = m.get("time")
        if m.get("secondsFromStart") is not None:
            t_sec = float(m["secondsFromStart"])
        elif t_ms and started_at_ms:
            t_sec = round((t_ms - started_at_ms) / 1000.0, 2)
        else:
            t_sec = 0.0
        speaker = "patient" if role == "bot" else "agent"
        turns.append({"t": round(t_sec, 2), "speaker": speaker, "text": text})
    return turns


def save_transcript(call_id: str, scenario: Scenario, call: dict) -> pathlib.Path | None:
    turns = _convert_messages(call)
    if not turns:
        log.warning("[%s] no turns in Vapi call; skipping transcript", call_id)
        return None
    started = call.get("startedAt") or call.get("createdAt")
    ended = call.get("endedAt")
    duration = call.get("durationSeconds")
    if duration is None and started and ended:
        try:
            t0 = dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
            t1 = dt.datetime.fromisoformat(ended.replace("Z", "+00:00"))
            duration = round((t1 - t0).total_seconds(), 2)
        except Exception:
            duration = None

    meta = {
        "call_id": call_id,
        "vapi_call_id": call.get("id"),
        "scenario": scenario.slug,
        "persona": f"{scenario.persona_name}, {scenario.persona_age}",
        "goal": scenario.goal,
        "edge_case": scenario.edge_case_note,
        "started_at_iso": started,
        "ended_at_iso": ended,
        "duration_sec": duration,
        "ended_reason": call.get("endedReason"),
        "turns": turns,
    }
    base = TRANSCRIPTS_DIR / call_id
    base.with_suffix(".json").write_text(json.dumps(meta, indent=2))

    lines = [
        f"# {scenario.slug} — {scenario.persona_name}",
        f"# Call ID: {call_id}  |  Vapi ID: {call.get('id')}",
        f"# Goal: {scenario.goal}",
        f"# Ended: {call.get('endedReason')}",
        "",
    ]
    for t in turns:
        who = "AGENT  " if t["speaker"] == "agent" else "PATIENT"
        lines.append(f"[{t['t']:>6.2f}s] {who} : {t['text']}")
    base.with_suffix(".txt").write_text("\n".join(lines))
    log.info("[%s] Transcript saved -> %s.{json,txt}", call_id, base)
    return base


async def download_recording(call: dict, call_id: str) -> pathlib.Path | None:
    """Pull the recording (if any) into recordings/<call_id>.mp3."""
    url = call.get("recordingUrl") or (call.get("artifact") or {}).get("recordingUrl")
    if not url:
        log.info("[%s] No recording URL on call object", call_id)
        return None
    dest = RECORDINGS_DIR / f"{call_id}.mp3"
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        dest.write_bytes(r.content)
    log.info("[%s] Recording saved -> %s (%d bytes)", call_id, dest, len(r.content))
    return dest
