"""Photon v2 credentials and authorization for one owner's direct conversation."""

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
    """An actionable error that never includes credential values."""


@dataclass(frozen=True, repr=False)
class PhotonConfig:
    key: SecretStr
    project: str
    owner: str
    agent: str

    @classmethod
    def load(cls, settings: Settings) -> PhotonConfig:
        key, project, owner, agent = (
            settings.photon_api_key,
            settings.photon_project_id,
            settings.photon_owner_handle,
            settings.photon_agent_handle,
        )
        if not all((key, project, owner, agent)):
            raise PhotonSetupError(
                "Set PHOTON_API_KEY, PHOTON_PROJECT_ID, PHOTON_OWNER_HANDLE and "
                "PHOTON_AGENT_HANDLE privately in the environment or ignored .env."
            )
        assert isinstance(key, SecretStr) and isinstance(project, str)
        assert isinstance(owner, str) and isinstance(agent, str)
        if not re.fullmatch(r"pho_prj_[A-Za-z0-9]+", project):
            raise PhotonSetupError("Use the Photon v2 project ID beginning pho_prj_.")
        if not re.fullmatch(r"pho_sk_[A-Za-z0-9_-]+", key.get_secret_value()):
            raise PhotonSetupError("Use a project-scoped Photon v2 key beginning pho_sk_.")
        if (
            any(c.isspace() or ord(c) < 32 for c in owner + agent)
            or not re.fullmatch(r"\+[1-9][0-9]{6,14}", agent)
            or not (
                re.fullmatch(r"\+[1-9][0-9]{6,14}", owner)
                or re.fullmatch(r"[^@<>,;]+@[^@<>,;]+\.[^@<>,;]+", owner)
            )
            or owner == agent
        ):
            raise PhotonSetupError(
                "Use your exact Messages send-from email or E.164 number, "
                "and the different number assigned to it in Photon v2."
            )
        return cls(key, project, owner, agent)


def owner_message(message: ReceivedImessageMessage, config: PhotonConfig) -> bool:
    """Unknown senders, groups, echoes and unsupported content never start work."""
    from photon_api.generated.models import TextContent, UserSnapshot

    try:
        if (
            datetime.fromisoformat(message.createdAt).utcoffset() is None
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
