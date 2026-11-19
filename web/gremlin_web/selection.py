"""CPU subject proposals for upright media, not tracking or mask validation.

The pinned YOLOX detector proposes human and animal characters on the reference and driving
first frame. Class names are hints, not identity restrictions. Clear dominant subjects are
selected; competing subjects ask for a point. Scores are heuristics, not probabilities.
The GPU engine remains responsible for segmentation, tracking and mask validity.
"""

from __future__ import annotations

import json
import os
from functools import cache
from pathlib import Path

import numpy as np
from PIL import Image

MODELS = Path(__file__).parent / "models"
DETECTOR = json.loads((MODELS / "detector.json").read_text())["name"]
SIZE = 416
# COCO classes that can be the character of a clip; anything else is scenery.
SUBJECTS = {
    0: "person",
    14: "bird",
    15: "cat",
    16: "dog",
    17: "horse",
    18: "sheep",
    19: "cow",
    20: "elephant",
    21: "bear",
    22: "zebra",
    23: "giraffe",
    77: "teddy bear",
}
# A detection this sure is offered as a candidate and counts as a competitor.
CANDIDATE_SCORE = 0.30
# An automatic choice needs at least this on the first frame.
SELECT_SCORE = 0.50


@cache
def _session():
    import onnxruntime

    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = os.cpu_count() or 1
    return onnxruntime.InferenceSession(
        str(MODELS / json.loads((MODELS / "detector.json").read_text())["file"]),
        options,
        providers=["CPUExecutionProvider"],
    )


@cache
def _grid():
    cells, strides = [], []
    for stride in (8, 16, 32):
        n = SIZE // stride
        ys, xs = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
        cells.append(np.stack([xs, ys], -1).reshape(-1, 2))
        strides.append(np.full((n * n, 1), stride))
    return np.concatenate(cells).astype(np.float32), np.concatenate(strides).astype(np.float32)


def iou(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    inter = max(0.0, w) * max(0.0, h)
    union = _area(a) + _area(b) - inter
    return inter / union if union > 0 else 0.0


def _area(box):
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _inside(point, box):
    return box[0] <= point["x"] <= box[2] and box[1] <= point["y"] <= box[3]


def detect(image):
    """Subject boxes in an RGB image, surest first: {"label", "score", "box"} dicts.

    Each box is [x0, y0, x1, y1] normalized to the image.
    """
    width, height = image.size
    scale = min(SIZE / width, SIZE / height)
    resized = image.convert("RGB").resize(
        (max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.BILINEAR
    )
    canvas = np.full((SIZE, SIZE, 3), 114, np.uint8)
    canvas[: resized.size[1], : resized.size[0]] = np.asarray(resized)[:, :, ::-1]
    session = _session()
    out = session.run(
        None, {session.get_inputs()[0].name: canvas.transpose(2, 0, 1)[None].astype(np.float32)}
    )[0][0]
    cells, strides = _grid()
    centers = (out[:, :2] + cells) * strides
    sizes = np.exp(out[:, 2:4]) * strides
    classes = np.array(sorted(SUBJECTS))
    scores = out[:, 4:5] * out[:, 5:][:, classes]
    best = scores.argmax(1)
    score = scores.max(1)
    keep = np.flatnonzero(score >= CANDIDATE_SCORE)
    boxes = np.concatenate([centers - sizes / 2, centers + sizes / 2], 1) / scale
    boxes = np.clip(boxes / [width, height, width, height], 0, 1)
    found = []
    for index in keep[np.argsort(-score[keep])]:
        label = SUBJECTS[int(classes[best[index]])]
        box = [round(float(v), 4) for v in boxes[index]]
        if _area(box) <= 0:
            continue
        # One box per subject: a weaker box over the same place is the same subject.
        if any(
            (f["label"] == "person") == (label == "person") and iou(f["box"], box) > 0.45
            for f in found
        ):
            continue
        found.append({"label": label, "score": round(float(score[index]), 3), "box": box})
    return found


def interior_point(box):
    """A point well inside the subject: the torso of a person, the middle of anything else.

    A box cut off by the bottom edge (head and shoulders) holds the torso lower down.
    """
    x0, y0, x1, y1 = box
    down = 0.65 if y1 >= 0.97 else 0.4
    return {"x": round((x0 + x1) / 2, 4), "y": round(y0 + down * (y1 - y0), 4)}


def _candidates(detections):
    return [
        {
            "label": d["label"],
            "score": d["score"],
            "box": d["box"],
            "point": interior_point(d["box"]),
        }
        for d in detections
    ]


def _clear_point(chosen, others):
    point = chosen["point"]
    return _inside(point, chosen["box"]) and not any(_inside(point, o["box"]) for o in others)


def _choose(image):
    candidates = _candidates(detect(image))
    result = {"candidates": candidates, "point": None, "subject": None}
    if not candidates:
        return result | {"why": "no_subject"}
    top = max(candidates, key=lambda c: _area(c["box"]))
    others = [c for c in candidates if c is not top]
    if top["score"] < SELECT_SCORE:
        return result | {"why": "unsure"}
    if any(_area(o["box"]) >= 0.25 * _area(top["box"]) for o in others):
        return result | {"why": "competing"}
    if not _clear_point(top, others):
        return result | {"why": "overlap"}
    return result | {"point": top["point"], "subject": candidates.index(top), "why": "auto"}


def choose_reference(image):
    """Propose one unambiguous human or animal character, otherwise ask for intent."""
    return _choose(image)


def analyze_clip(samples, duration_ms):
    """Propose the first-frame character; later detections cannot validate physical presence.

    The engine owns segmentation and tracking. Neither a missed box nor a second box proves
    departure, and a proposal must never change the selected action's duration.
    """
    return _choose(samples[0][1]) | {"duration_ms": duration_ms}


def tier(candidate, caption):
    """Caption-based suggestion, never a claim of quality for an unqualified scene."""
    if (
        candidate is not None
        and candidate["label"] == "person"
        and caption["reference_kind"] == "person"
        and caption["driving_kind"] == "person"
        and caption["motion"] == "everyday"
    ):
        return "simple-motion"
    return "complex-motion"
