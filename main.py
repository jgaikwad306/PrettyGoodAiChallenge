"""CLI orchestrator (Vapi edition).

No local server or tunnel required — Vapi runs the whole call in their cloud.
You just need a Vapi account, an assigned phone number, and provider
credentials (Anthropic / Deepgram / ElevenLabs) configured in the Vapi
dashboard.

Typical usage:

    python main.py list
    python main.py call schedule-new
    python main.py run-all
    python main.py analyze <call_id>
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import pathlib
import sys
import time
import uuid

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
)
log = logging.getLogger("cli")
console = Console()

from analyzer import analyze_transcript  # noqa: E402
from scenarios import ALL_SCENARIOS, SCENARIOS_BY_SLUG  # noqa: E402
from vapi_client import (  # noqa: E402
    download_recording,
    place_call,
    save_transcript,
    wait_for_call,
)


REPO = pathlib.Path(__file__).parent

REQUIRED_ENV = (
    "VAPI_API_KEY",
    "VAPI_PHONE_NUMBER_ID",
    "ANTHROPIC_API_KEY",
    "TARGET_NUMBER",
    "ELEVENLABS_VOICE_ID",
)


def _require_env() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        console.print(f"[red]Missing env vars: {', '.join(missing)}[/red]")
        console.print("Copy .env.example -> .env and fill in your keys.")
        sys.exit(1)


def _new_call_id(scenario_slug: str) -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{scenario_slug}-{uuid.uuid4().hex[:6]}"


@click.group()
def cli():
    """PGAI voice-bot orchestrator (Vapi)."""


@cli.command("list")
def list_scenarios():
    """Show all configured scenarios."""
    t = Table(title="Scenarios")
    t.add_column("Slug", style="cyan")
    t.add_column("Persona")
    t.add_column("Goal")
    t.add_column("Edge?", style="yellow")
    for s in ALL_SCENARIOS:
        t.add_row(s.slug, f"{s.persona_name}, {s.persona_age}", s.goal, "yes" if s.edge_case_note else "")
    console.print(t)


@cli.command("call")
@click.argument("scenario_slug")
@click.option("--max-wait", default=360, help="Seconds to wait for call to complete.")
@click.option("--no-analyze", is_flag=True, help="Skip post-call analysis.")
def call_one(scenario_slug: str, max_wait: int, no_analyze: bool):
    """Place a single call for SCENARIO_SLUG."""
    _require_env()
    if scenario_slug not in SCENARIOS_BY_SLUG:
        console.print(f"[red]Unknown scenario '{scenario_slug}'. Try `python main.py list`.[/red]")
        sys.exit(1)
    asyncio.run(_run_single_call(scenario_slug, max_wait=max_wait, analyze=not no_analyze))


@cli.command("run-all")
@click.option("--max-wait", default=360, help="Seconds to wait per call.")
@click.option("--gap", default=10, help="Seconds between calls.")
@click.option("--no-analyze", is_flag=True, help="Skip per-call analysis.")
def run_all(max_wait: int, gap: int, no_analyze: bool):
    """Place every scenario back-to-back."""
    _require_env()

    async def _runner():
        for i, s in enumerate(ALL_SCENARIOS, 1):
            console.rule(f"[{i}/{len(ALL_SCENARIOS)}] {s.label()}")
            try:
                await _run_single_call(s.slug, max_wait=max_wait, analyze=not no_analyze)
            except Exception:
                log.exception("Call for %s failed; continuing", s.slug)
            if i < len(ALL_SCENARIOS):
                console.print(f"[dim]Waiting {gap}s before next call...[/dim]")
                time.sleep(gap)

    asyncio.run(_runner())


@cli.command("analyze")
@click.argument("call_id")
def analyze(call_id: str):
    """Re-run the bug analyzer on an existing transcript."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        console.print("[red]Missing ANTHROPIC_API_KEY[/red]")
        sys.exit(1)
    findings = analyze_transcript(call_id)
    console.print(findings)


async def _run_single_call(scenario_slug: str, *, max_wait: int, analyze: bool) -> None:
    scenario = SCENARIOS_BY_SLUG[scenario_slug]
    call_id = _new_call_id(scenario_slug)
    target = os.environ["TARGET_NUMBER"]
    console.print(f"[green]Placing call[/green] call_id=[cyan]{call_id}[/cyan] -> [magenta]{target}[/magenta]")

    vapi_id = await place_call(scenario, target_number=target)
    console.print(f"Vapi call: [magenta]{vapi_id}[/magenta]")

    call = await wait_for_call(vapi_id, max_wait=max_wait)
    status = call.get("status", "?")
    ended_reason = call.get("endedReason", "?")
    console.print(f"Final status: [bold]{status}[/bold]  reason: [yellow]{ended_reason}[/yellow]")

    try:
        rec = await download_recording(call, call_id)
        if rec:
            console.print(f"Recording: [green]{rec}[/green]")
    except Exception:
        log.exception("Recording download failed")

    saved = save_transcript(call_id, scenario, call)
    if not saved:
        console.print("[yellow]No transcript saved — analyzer skipped.[/yellow]")
        return

    if analyze:
        try:
            console.rule("[bold]Bug Analyzer[/bold]")
            findings = analyze_transcript(call_id)
            console.print(findings)
        except Exception:
            log.exception("Analyzer failed")


if __name__ == "__main__":
    cli()
