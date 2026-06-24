"""Scenario data model — one Scenario per test call."""

from dataclasses import dataclass, field


@dataclass
class Scenario:
    slug: str
    persona_name: str
    persona_age: int
    persona_background: str
    goal: str
    edge_case_note: str = ""
    voice_id: str | None = None
    max_turns: int = 24
    extra_rules: list[str] = field(default_factory=list)

    def label(self) -> str:
        tag = " (edge)" if self.edge_case_note else ""
        return f"{self.slug}: {self.persona_name}, {self.persona_age}{tag}"
