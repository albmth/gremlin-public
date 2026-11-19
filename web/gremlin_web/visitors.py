"""Anonymous visitors, the shared allowance and the upload limit.

There are no accounts. A visitor is a browser holding a random 256-bit token in an HttpOnly
cookie; its ID is a hash of that token, so it cannot be guessed from anything the site shows.
A visitor's drafts and in-progress generations are scoped to that ID.

Generations draw on one allowance shared by everyone, first come, first served, until an operator
replenishes it, and on a daily allowance per network address so one visitor can't take it all.
Each reservation is written in the same transaction as the action it admits, so concurrent
requests cannot exceed either.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time

from gremlin_web.db import Txn

COOKIE = "gremlin_visitor"
COOKIE_SECONDS = 30 * 86400
DAY = 86400
TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def visitor_of(token):
    """The visitor ID for a cookie value, or None when it is not one of ours."""
    if not token or not TOKEN.fullmatch(token):
        return None
    return hashlib.sha256(token.encode()).hexdigest()[:32]


def new_token():
    return secrets.token_urlsafe(32)


class LimitReached(Exception):
    def __init__(self, message, code="quota"):
        super().__init__(message)
        self.message = message
        self.code = code


class Quotas:
    ALLOWANCE = {"pk": "QUOTA", "sk": "allowance"}

    def __init__(self, table, clock, total, daily_per_address):
        self.table = table
        self.clock = clock
        self.total = total
        self.daily_per_address = daily_per_address

    # The shared generation allowance

    def _allowance(self):
        return self.table.get(self.ALLOWANCE["pk"], self.ALLOWANCE["sk"]) or self.ALLOWANCE | {
            "n": 0
        }

    def left(self):
        return max(0, self.total - self._allowance()["n"])

    def reserve(self, txn, address):
        """Stage one generation in `txn`, or raise LimitReached when none is left. Returns the
        key of the address's day counter it charged, which `release` needs, or None."""
        item = self._allowance()
        if item["n"] >= self.total:
            raise LimitReached("No videos are left.")
        txn.put(item | {"n": item["n"] + 1})
        if not address:
            return None
        day = self._item("videos", address, DAY)
        if day["n"] >= self.daily_per_address:
            raise LimitReached(
                "This network has used today's videos. Try again tomorrow (UTC).", "network_quota"
            )
        txn.put(day | {"n": day["n"] + 1})
        return day["sk"]

    def release(self, txn, day_key=None):
        """Stage the return of a reservation that cost nothing."""
        item = self._allowance()
        # A replenishment since the reservation already gave everything back.
        if item["n"] > 0:
            txn.put(item | {"n": item["n"] - 1})
        if day_key and (day := self.table.get("QUOTA", day_key)) and day["n"] > 0:
            txn.put(day | {"n": day["n"] - 1})

    def network_left(self, address):
        return max(0, self.daily_per_address - self.used("videos", address, DAY))

    def replenish(self):
        """Give the whole allowance back; the operator's action."""
        txn = Txn(self.table)
        # Unconditional: a generation reserved meanwhile is forgiven too.
        txn.delete(self.ALLOWANCE)
        txn.commit()
        return self.total

    # Uploads per network address, in fixed UTC windows

    def _item(self, scope, subject, window):
        bucket = int(self.clock() // window)
        key = {"pk": "QUOTA", "sk": f"{scope}#{subject}#{bucket}"}
        return self.table.get(key["pk"], key["sk"]) or key | {
            "n": 0,
            "expire_at": int((bucket + 2) * window),
        }

    def used(self, scope, subject, window):
        return self._item(scope, subject, window)["n"]

    def charge(self, txn, scope, subject, window, limit, message):
        """Stage one more use in `txn`, or raise LimitReached when the window is full."""
        item = self._item(scope, subject, window)
        if item["n"] >= limit:
            raise LimitReached(message)
        txn.put(item | {"n": item["n"] + 1})


class CaptionBudget:
    """The scene-description spending caps, in micro-USD per UTC day and per UTC month.

    Each call reserves `RESERVATION` on both counters in the transaction that records the call,
    so concurrent calls cannot overspend; once Replicate reports the call's token usage the
    difference to the actual charge is settled. A call without an answer may still have been
    billed, so its reservation is never settled: the conservative charge stands.
    """

    # One call's worst case: about 3,800 input and at most 2,000 output tokens.
    RESERVATION = 6200

    def __init__(self, table, clock, daily, monthly):
        self.table = table
        self.clock = clock
        self.limits = {"day": daily, "month": monthly}

    def _keys(self):
        now = time.gmtime(self.clock())
        return {
            "day": f"caption#day#{now.tm_year:04d}{now.tm_mon:02d}{now.tm_mday:02d}",
            "month": f"caption#month#{now.tm_year:04d}{now.tm_mon:02d}",
        }

    def _item(self, sk):
        # Day counters outlive their day by two, month counters their month by about two.
        keep = 3 * 86400 if sk.startswith("caption#day#") else 62 * 86400
        return self.table.get("BUDGET", sk) or {
            "pk": "BUDGET",
            "sk": sk,
            "n": 0,
            "expire_at": int(self.clock() + keep),
        }

    def remaining(self):
        return min(self.limits[p] - self._item(sk)["n"] for p, sk in self._keys().items())

    def reserve(self, txn):
        """Stage one call's reservation in `txn`; its counter keys, or raises LimitReached."""
        keys = self._keys()
        items = {period: self._item(sk) for period, sk in keys.items()}
        for period, item in items.items():
            if item["n"] + self.RESERVATION > self.limits[period]:
                raise LimitReached(f"The {period}'s scene-description budget is used up.")
        for item in items.values():
            txn.put(item | {"n": item["n"] + self.RESERVATION})
        return list(keys.values())

    def settle(self, txn, keys, actual):
        """Stage the correction from the reservation to the call's `actual` charge."""
        for sk in keys:
            item = self._item(sk)
            txn.put(item | {"n": item["n"] + actual - self.RESERVATION})
