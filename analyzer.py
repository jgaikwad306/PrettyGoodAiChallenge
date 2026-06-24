"""Post-call analyzer. Reads a saved transcript JSON, asks Claude to flag bugs,
and appends findings to bug_report.md.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib

from anthropic import Anthropic

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
REPO_ROOT = pathlib.Path(__file__).parent
BUG_REPORT_PATH = REPO_ROOT / "bug_report.md"
TRANSCRIPTS_DIR = REPO_ROOT / "transcripts"


SYSTEM = """You are a senior QA engineer reviewing a phone call transcript between an AI
voice agent for a medical office and a simulated patient.

Your job: surface real bugs, quality issues, or policy violations the AI agent
made. Skip nitpicks (punctuation, minor word choice). Focus on things like:

- Wrong factual info (hours, dates, policies)
- Booking a time that contradicts office hours
- Handling of controlled substances or refills incorrectly
- Missing a safety escalation (e.g. chest pain -> ER)
- Misunderstanding the patient and proceeding anyway
- Breaking the conversation flow, looping, or contradicting itself
- Not handling silence, mid-call topic pivots, or ambiguous input
- Confidently stating something it has no way to know

For each issue: be specific, quote the agent line, give a severity (Low/Medium/High),
and say why it matters. If nothing notable happened, say "no issues found"."""

USER_TEMPLATE = """Scenario: {slug} — {persona}
Goal of the test call: {goal}
Edge-case note: {edge}

Transcript (timestamps in seconds; AGENT is the PGAI bot under test, PATIENT is our simulator):

{transcript}

Produce a bulleted list of issues. For each:
- **Severity**: High/Medium/Low
- **What happened** (1 sentence)
- **Quote**: agent line, with timestamp
- **Why it matters** (1 sentence)

If there are no notable issues, output exactly: NO ISSUES FOUND."""


def analyze_transcript(call_id: str) -> str:
    json_path = TRANSCRIPTS_DIR / f"{call_id}.json"
    if not json_path.exists():
        raise FileNotFoundError(f"No transcript at {json_path}")
    data = json.loads(json_path.read_text())
    lines = []
    for t in data["turns"]:
        who = "AGENT  " if t["speaker"] == "agent" else "PATIENT"
        lines.append(f"[{t['t']:>6.2f}s] {who} : {t['text']}")
    transcript_block = "\n".join(lines) or "(no audio captured)"

    user = USER_TEMPLATE.format(
        slug=data["scenario"],
        persona=data["persona"],
        goal=data["goal"],
        edge=data.get("edge_case") or "(none)",
        transcript=transcript_block,
    )

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    findings = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()

    _append_bug_report(call_id=call_id, meta=data, findings=findings)
    return findings


def _append_bug_report(*, call_id: str, meta: dict, findings: str) -> None:
    header = (
        f"\n\n---\n\n"
        f"## Call: `{call_id}` — scenario `{meta['scenario']}`\n"
        f"- Persona: {meta['persona']}\n"
        f"- Goal: {meta['goal']}\n"
        f"- Recording: `recordings/{call_id}.mp3`\n"
        f"- Transcript: `transcripts/{call_id}.txt`\n\n"
    )
    if not BUG_REPORT_PATH.exists():
        BUG_REPORT_PATH.write_text(
            "# PGAI Voice Bot — Bug Report\n\n"
            "Auto-generated after each call by `analyzer.py`. "
            "Reviewed entries are kept in `bug_report_curated.md`.\n"
        )
    with BUG_REPORT_PATH.open("a") as f:
        f.write(header)
        f.write(findings)
    log.info("Appended findings for %s to %s", call_id, BUG_REPORT_PATH.name)


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("usage: python analyzer.py <call_id>")
        sys.exit(1)
    print(analyze_transcript(sys.argv[1]))
