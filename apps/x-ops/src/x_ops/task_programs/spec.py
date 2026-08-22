from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProgramSpec:
    name: str
    version: str
    title: str
    description: str
    preserve_browser_on_uncertain: bool = False
    supports_batch_schedule: bool = True
