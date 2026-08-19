from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProgramSpec:
    name: str
    version: str
    title: str
    description: str
