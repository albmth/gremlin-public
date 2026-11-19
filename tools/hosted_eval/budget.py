"""Decimal reservations remain committed until authoritative reconciliation."""

import json
from decimal import Decimal

from .ledger import Hold, write_json

HARD_CAP = Decimal("2.00")


def money(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise Hold("Invalid money amount")
    return result


class Budget:
    def __init__(self, root, ledger, *, hard_cap=HARD_CAP, scope="Phase A only"):
        self.root, self.ledger = root, ledger
        self.hard_cap, self.scope = money(hard_cap), scope

    def approve(self, amount):
        amount = money(amount)
        if not 0 < amount <= self.hard_cap:
            raise Hold(f"Approval must be positive and at most USD {self.hard_cap}")
        path = self.root / "budget.json"
        if path.exists():
            if money(json.loads(path.read_text())["cap_usd"]) != amount:
                raise Hold("Existing approval differs; cannot reset campaign authority")
            return
        write_json(path, {"cap_usd": str(amount), "scope": self.scope})
        self.ledger.append("approved", cap_usd=str(amount))

    def total(self):
        total = Decimal(0)
        for intent in self.ledger.select("intent"):
            settlements = [
                e for e in self.ledger.select("posted") if e["attempt"] == intent["attempt"]
            ]
            estimates = [
                e for e in self.ledger.select("terminal") if e["attempt"] == intent["attempt"]
            ]
            reserve = money(intent["reserve_usd"])
            # A posted bill may only raise the conservative commitment.
            if settlements:
                reserve = max(reserve, money(settlements[-1]["posted_usd"]))
            elif estimates and estimates[-1].get("metric_usd") is not None:
                reserve = max(reserve, money(estimates[-1]["metric_usd"]))
            total += reserve
        return total

    def likely_total(self):
        total = Decimal(0)
        for intent in self.ledger.select("intent"):
            posted = [e for e in self.ledger.select("posted") if e["attempt"] == intent["attempt"]]
            terminal = [
                e for e in self.ledger.select("terminal") if e["attempt"] == intent["attempt"]
            ]
            if posted:
                total += money(posted[-1]["posted_usd"])
            elif terminal and terminal[-1]["status"] == "succeeded":
                total += money(terminal[-1]["metric_usd"])
            elif not terminal:
                total += money(intent["reserve_usd"])
        return total

    def reserve(self, amount):
        path = self.root / "budget.json"
        if not path.exists():
            raise Hold(f"Paid calls require approve --max-usd (at most {self.hard_cap})")
        cap = money(json.loads(path.read_text())["cap_usd"])
        if not 0 < cap <= self.hard_cap or self.total() + money(amount) > cap:
            raise Hold("Campaign spending ceiling would be exceeded")
