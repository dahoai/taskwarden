"""The columns taskwarden reads and writes, as SQLAlchemy mixins for a new app's tables.

    class Task(Base, TaskColumns):
        __tablename__ = "team_tasks"

An app that already has its tables keeps them, as long as they carry every column here. `organization_id` is the business: add a foreign key in your own model.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

_JSON = JSON().with_variant(JSONB(), "postgresql")


class TaskColumns:
    """One unit of team work: an owner's request, a hand-off, or a scheduled run. `state` is
    the conversation so far; `pending` what it waits on (an approval, a question, a teammate)."""
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[str] = mapped_column(String, nullable=False)
    agent_id: Mapped[str] = mapped_column(String(50), nullable=False)
    thread: Mapped[str] = mapped_column(String(50), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="queued")
    state: Mapped[list] = mapped_column(_JSON, nullable=False, default=list)
    pending: Mapped[Optional[dict[str, Any]]] = mapped_column(_JSON, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    origin: Mapped[str] = mapped_column(String(10), nullable=False, default="owner")
    # What this task already sent ("tool|recipient|body"): never the same message twice.
    contacted: Mapped[list] = mapped_column(_JSON, nullable=False, default=list)
    # Model tokens (in and out) across every run of the task: what a daily budget sums.
    tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # The caller's own id for the event that started this task (a webhook's delivery id): the
    # same one twice starts one task. Add a unique index on (organization_id, agent_id,
    # request_id) so even two deliveries in the same instant cannot both land.
    # The version of the app's task state this task was saved under (host.STATE_VERSION): a task
    # parked before a deploy that changed the shape of its state is migrated or stopped, not
    # resumed wrongly. None = not stamped yet, treated as current.
    state_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Short hashes of the system prompts this task ran under, oldest first (the last 20): with
    # host.save_prompt, "what did the model see when it sent that?" has an answer.
    prompt_hashes: Mapped[list] = mapped_column(_JSON, nullable=False, default=list)
    request_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())

    @declared_attr
    def parent_id(cls) -> Mapped[Optional[uuid.UUID]]:
        from sqlalchemy import ForeignKey
        return mapped_column(UUID(as_uuid=True), ForeignKey(f"{cls.__tablename__}.id"), nullable=True)


class MessageColumns:
    """A message in a chat: the team room, or one agent's own chat (thread = agent id)."""
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[str] = mapped_column(String, nullable=False)
    thread: Mapped[str] = mapped_column(String(50), nullable=False)
    sender: Mapped[str] = mapped_column(String(50), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False, default="text")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
