from dataclasses import dataclass
from enum import Enum
from typing import Optional


class UserRole(str, Enum):
    ADMIN = "admin"
    VIEWER = "viewer"


@dataclass
class User:
    id: int
    username: str
    password_hash: str
    role: UserRole
    created_at: float
    updated_at: float

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class AuditEntry:
    id: int
    user_id: Optional[str]
    action: str
    ip_address: Optional[str]
    user_agent: Optional[str]
    created_at: float

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "action": self.action,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "created_at": self.created_at,
        }
