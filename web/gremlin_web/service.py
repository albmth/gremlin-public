"""Builds the application's components and runs the worker's scheduled and operator work."""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from gremlin_web.db import Media, Table
from gremlin_web.generations import Generations
from gremlin_web.gremlin import Gremlin
from gremlin_web.visitors import CaptionBudget, Quotas

log = logging.getLogger("gremlin_web")


class Service:
    def __init__(self, settings, clock=time.time):
        self.settings = settings
        self.clock = clock
        self.table = Table(settings)
        self.media = Media(settings)
        token = settings.gremlin_token
        self.gremlin = (
            Gremlin(settings.gremlin_base_url, token.get_secret_value())
            if settings.gremlin_base_url and token
            else None
        )
        self.quotas = Quotas(
            self.table, clock, settings.total_generations, settings.address_daily_videos
        )
        self.budget = CaptionBudget(
            self.table, clock, settings.caption_daily_microusd, settings.caption_monthly_microusd
        )
        self.generations = Generations(
            settings,
            self.table,
            self.media,
            self.gremlin,
            self.quotas,
            self.budget,
            clock,
            self.invoke,
        )
        self._pool = ThreadPoolExecutor(4) if settings.role == "local" else None
        self._lambda = None

    def invoke(self, visitor, gid):
        """Advance a generation soon without blocking the caller."""
        if self._pool is not None:
            self._pool.submit(self._process_logged, visitor, gid)
            return
        if self._lambda is None:
            import boto3

            self._lambda = boto3.client("lambda", region_name=self.settings.region)
        self._lambda.invoke(
            FunctionName=self.settings.worker_function,
            InvocationType="Event",
            Payload=json.dumps({"web": "process", "visitor": visitor, "gid": gid}).encode(),
        )

    def _process_logged(self, visitor, gid):
        try:
            self.generations.process(visitor, gid)
        except Exception:
            log.exception("local processing of %s failed", gid)

    def sweep(self):
        """The one-minute schedule: advance the active generation when it is due, end pending
        choices past their deadline, and delete expired results and abandoned uploads."""
        report = {"processed": 0, "expired": self.generations.expire_selections()}
        for visitor, gid in self.generations.due():
            if self._pool is None:
                # Its own invocation and time budget, so the sweep stays short.
                self.invoke(visitor, gid)
            else:
                self.generations.process(visitor, gid)
            report["processed"] += 1
        report["results_deleted"] = self.generations.expire_results()
        report["drafts_deleted"] = self.generations.expire_drafts()
        report["healthy"] = True
        return report

    def operate(self, event):
        """Operator actions, invoked by the deployer through the worker function."""
        action = event.get("web_operator")
        if action == "state":
            slot = self.generations.active()
            return {
                "build": self.settings.build_commit,
                "environment": self.settings.environment,
                "active_generation": slot and slot["gid"],
                "videos_left": self.quotas.left(),
                "total_generations": self.settings.total_generations,
                "caption_remaining_microusd": self.budget.remaining(),
                "gremlin": self.generations.admission(),
            }
        if action == "sweep":
            return self.sweep()
        if action == "replenish":
            return {"videos_left": self.quotas.replenish()}
        if action == "remove_result":
            return {"removed": self.generations.remove_result(event.get("gid", ""))}
        return {"error": f"unknown action {action}"}


def run_local_sweeper(service, interval=3.0):
    """The local server's stand-in for the one-minute schedule."""

    def loop():
        while True:
            try:
                service.sweep()
            except Exception:
                log.exception("local sweep failed")
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True, name="gremlin-web-sweep").start()
