from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ..models import AccountRecord


@dataclass(frozen=True, slots=True)
class AccountContext:
    account_id: str
    name: str
    username: str | None
    tags: tuple[str, ...]
    metadata: Mapping[str, Any]

    @classmethod
    def from_record(cls, account: AccountRecord) -> AccountContext:
        return cls(
            account_id=account.id,
            name=account.name,
            username=account.username,
            tags=tuple(account.tags),
            metadata=MappingProxyType(dict(account.metadata)),
        )
