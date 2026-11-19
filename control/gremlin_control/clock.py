"""Every service clock reads real time plus an offset that only the test hooks can change."""

import time

_offset = 0.0
_frozen = None


def now():
    return _frozen if _frozen is not None else time.time() + _offset


def set_offset(seconds):
    global _offset, _frozen
    _offset, _frozen = float(seconds), None


def freeze(at):
    global _frozen
    _frozen = float(at)
