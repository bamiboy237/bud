"""Photon 2.0 configuration and the single owner-message authorization rule."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import SecretStr

from bud.config import Settings

if TYPE_CHECKING:
    from photon_api.generated.models import ReceivedImessageMessage


class PhotonSetupError(Exception):
    """A credential-free actionable setup error."""


@dataclass(frozen=True, repr=False)
class PhotonConfig:
    key: SecretStr
    project: str
    owner: str
    agent: str

    @classmethod
    def load(cls, settings: Settings) -> PhotonConfig:
        values = (
            settings.photon_api_key,
            settings.photon_project_id,
            settings.photon_owner_handle,
            settings.photon_agent_handle,
        )
        if not all(values):
            raise PhotonSetupError(
                "Set PHOTON_API_KEY, PHOTON_PROJECT_ID, PHOTON_OWNER_HANDLE and "
                "PHOTON_AGENT_HANDLE privately in the environment or ignored .env. "
                "Never paste the API key into chat."
            )
        key, project, owner, agent = values
        assert isinstance(key, SecretStr)
        assert isinstance(project, str) and isinstance(owner, str) and isinstance(agent, str)
        if not re.fullmatch(r"pho_prj_[A-Za-z0-9]+", project):
            raise PhotonSetupError("Use a Photon 2.0 project ID beginning pho_prj_.")
        if not re.fullmatch(r"pho_sk_[A-Za-z0-9_-]+", key.get_secret_value()):
            raise PhotonSetupError("Use a project-scoped Photon 2.0 API key beginning pho_sk_.")
        if (
            owner != owner.strip()
            or agent != agent.strip()
            or any(c.isspace() or ord(c) < 32 for c in owner + agent)
            or not re.fullmatch(r"\+[1-9][0-9]{6,14}", agent)
            or not (
                re.fullmatch(r"\+[1-9][0-9]{6,14}", owner)
                or re.fullmatch(r"[^@<>,;]+@[^@<>,;]+\.[^@<>,;]+", owner)
            )
            or owner == agent
        ):
            raise PhotonSetupError(
                "Use the exact Messages send-from email or E.164 number for PHOTON_OWNER_HANDLE, "
                "and the different Photon-assigned E.164 number for PHOTON_AGENT_HANDLE."
            )
        return cls(key, project, owner, agent)


def owner_message(message: ReceivedImessageMessage, config: PhotonConfig) -> bool:
    """Accept only plain text in the exact human→agent direct conversation."""
    from photon_api.generated.models import TextContent, UserSnapshot

    try:
        created = datetime.fromisoformat(message.createdAt)
        if (
            created.utcoffset() is None
            or message.projectId != config.project
            or not isinstance(message.receivedBy, UserSnapshot)
        ):
            return False
        sender, agent, space = message.sentBy.root, message.receivedBy.root, message.space.root
        return (
            message.platform == sender.platform == agent.platform == space.platform == "imessage"
            and sender.projectId == agent.projectId == space.projectId == config.project
            and message.status == "received"
            and space.kind == "direct"
            and sender.kind == "human"
            and sender.handle == config.owner
            and agent.kind == "agent"
            and agent.handle == config.agent
            and bool(sender.id and agent.id and space.id and message.id)
            and sender.id != agent.id
            and isinstance(message.content, TextContent)
            and message.content.format == "plain"
            and bool(message.content.text.strip())
        )
    except (ValueError, AttributeError, TypeError):
        return False
