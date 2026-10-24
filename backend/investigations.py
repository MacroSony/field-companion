"""Session-local background investigations, independent of the voice turn worker."""
import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import math
import os
from pathlib import Path
import time

from dotenv import dotenv_values

LOG = logging.getLogger("field-companion.investigations")
ACTIVE = {"queued", "running"}
TERMINAL = {"completed", "failed", "timed_out", "cancelled"}


class QueueFull(ValueError):
    pass


def stamp():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class InvestigationSettings:
    timeout: float = 180
    request_timeout: float = 120
    capacity: int = 5
    history: int = 20

    @classmethod
    def load(cls):
        try:
            local = dotenv_values(Path(__file__).resolve().parents[1] / ".env.local")
        except (OSError, UnicodeError):
            local = {}

        def seconds(name, default, maximum):
            try:
                value = float(os.environ.get(name, local.get(name) or default))
                return max(1, min(maximum, value)) if math.isfinite(value) else default
            except (ValueError, TypeError):
                return default

        timeout = seconds("INVESTIGATION_TIMEOUT_SECONDS", 180, 900)
        return cls(timeout=timeout, request_timeout=min(timeout,
            seconds("INVESTIGATION_REQUEST_TIMEOUT_SECONDS", 120, 600)))


@dataclass
class Investigation:
    id: str
    question: str
    observation: dict = field(repr=False)
    origin: str = "question"
    parent_id: str | None = None
    status: str = "queued"
    stage: str = "queued"
    created_at: str = field(default_factory=stamp)
    started_at: str | None = None
    finished_at: str | None = None
    started: float | None = None
    elapsed_seconds: float = 0
    result: dict | None = field(default=None, repr=False)
    announced: bool = False
    notice_attempts: int = 0

    def payload(self, position=0):
        top = self.observation.get("fused", [])[:1]
        species = top[0].get("en") or top[0].get("sci") if top else ""
        return {"id": self.id, "question": self.question, "origin": self.origin,
                "parent_id": self.parent_id, "observation_id": self.observation["id"],
                "species": species, "status": self.status, "stage": self.stage,
                "created_at": self.created_at, "started_at": self.started_at,
                "finished_at": self.finished_at, "queue_position": position,
                "elapsed_seconds": round(time.monotonic() - self.started, 1)
                    if self.status == "running" and self.started is not None else round(self.elapsed_seconds, 1),
                "announced": self.announced, "result": deepcopy(self.result)}


class InvestigationQueue:
    """One runner per session; callbacks publish state and enqueue voice notices only."""
    def __init__(self, runner, on_change, on_done, settings=None):
        self.runner, self.on_change, self.on_done = runner, on_change, on_done
        self.settings = settings or InvestigationSettings.load()
        self.jobs = {}
        self.sequence = 0
        self.worker = self.running_task = None
        self.running_id = None
        self.closed = False

    def payload(self):
        queued = [j.id for j in self.jobs.values() if j.status == "queued"]
        return {"type": "investigations", "items": [
            job.payload(queued.index(job.id) + 1 if job.id in queued else 0)
            for job in self.jobs.values()], "capacity": self.settings.capacity,
            "timeout_seconds": self.settings.timeout}

    async def emit(self):
        if not self.closed:
            try:
                await self.on_change(self.payload())
            except Exception as exc:
                LOG.warning("investigation_publish_failed error_type=%s", type(exc).__name__)

    async def submit(self, question, observation, *, origin="question", parent_id=None):
        if self.closed:
            raise ValueError("Investigations are closed")
        if not isinstance(question, str) or not question.strip() or len(question) > 2000:
            raise ValueError("Investigation question must contain 1–2000 characters")
        snapshot = deepcopy(observation)
        snapshot["detections"] = snapshot.get("detections", [])[-12:]
        snapshot["fused"] = snapshot.get("fused", [])[:5]
        for job in self.jobs.values():
            if job.status in ACTIVE and job.question == question and job.observation == snapshot:
                return job
        if sum(j.status in ACTIVE for j in self.jobs.values()) >= self.settings.capacity:
            raise QueueFull("Investigation queue is full; cancel a task or wait for a result")
        while len(self.jobs) >= self.settings.history:
            oldest = next((j.id for j in self.jobs.values() if j.status in TERMINAL), None)
            if oldest is None:
                break
            del self.jobs[oldest]
        self.sequence += 1
        job = Investigation("R" + str(self.sequence), question, snapshot, origin, parent_id)
        self.jobs[job.id] = job
        await self.emit()
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self.run())
        return job

    def select(self, task_id=None, selection=None, *, active=False):
        rows = list(self.jobs.values())
        if task_id:
            job = self.jobs.get(str(task_id).upper())
            if job is None:
                raise ValueError("Unknown investigation")
            return [job]
        if selection not in (None, "first", "latest", "all"):
            raise ValueError("Invalid investigation selection")
        if active:
            rows = [j for j in rows if j.status in ACTIVE]
        if selection == "first":
            return [j for j in rows if j.id == "R1"]
        if selection == "latest":
            return rows[-1:]
        return rows

    async def cancel(self, job):
        if job.status not in ACTIVE:
            return False
        job.status = job.stage = "cancelled"
        job.finished_at = stamp()
        job.elapsed_seconds = time.monotonic() - job.started if job.started is not None else 0
        job.announced = True
        if self.running_id == job.id and self.running_task is not None:
            self.running_task.cancel()
        await self.emit()
        return True

    async def execute(self, job):
        async def progress(stage):
            if job.status == "running" and stage in ("retrieving", "analyzing"):
                job.stage = stage
                await self.emit()

        try:
            async with asyncio.timeout(self.settings.timeout):
                result = await self.runner(job.question, deepcopy(job.observation), progress)
            if job.status != "running":
                return  # A cancelled runner must never publish a late success.
            job.result = deepcopy(result)
            job.status = "completed" if result.get("ok") else (
                "timed_out" if result.get("status") == "timeout" else "failed")
        except TimeoutError:
            if job.status != "running":
                return
            job.status = "timed_out"
            job.result = {"ok": False, "status": "timeout"}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if job.status != "running":
                return
            LOG.warning("investigation_failed error_type=%s", type(exc).__name__)
            job.status = "failed"
            job.result = {"ok": False, "status": "unavailable"}
        job.stage = job.status
        job.finished_at = stamp()
        job.elapsed_seconds = time.monotonic() - job.started
        await self.emit()
        if not self.closed:
            try:
                await self.on_done(job)
            except Exception as exc:
                LOG.warning("investigation_notice_failed error_type=%s", type(exc).__name__)

    async def run(self):
        while not self.closed:
            job = next((j for j in self.jobs.values() if j.status == "queued"), None)
            if job is None:
                return
            job.status, job.stage = "running", "retrieving"
            job.started, job.started_at = time.monotonic(), stamp()
            self.running_id = job.id
            self.running_task = asyncio.create_task(self.execute(job))
            try:
                await self.emit()
                await self.running_task
            except asyncio.CancelledError:
                if self.closed:
                    raise
            finally:
                self.running_id = self.running_task = None

    async def close(self):
        self.closed = True
        for job in self.jobs.values():
            if job.status in ACTIVE:
                job.status = job.stage = "cancelled"
                job.finished_at = stamp()
        tasks = [t for t in (self.running_task, self.worker) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
