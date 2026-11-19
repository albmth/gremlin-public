"""Ground-truth measurements of a candidate against the driving clip.

These support the owner's review and never replace it: they say how much of the scene, the
camera and the subject's motion the candidate kept, not whether it looks right.
"""

import av
import cv2
import numpy as np

FLOW_SIZE = 256
BACKGROUND_MARGIN_PX = 12


def frames_of(path, count, size=512):
    frames = []
    with av.open(str(path)) as container:
        for frame in container.decode(container.streams.video[0]):
            rgb = frame.to_ndarray(format="rgb24")
            if rgb.shape[:2] != (size, size):
                rgb = cv2.resize(rgb, (size, size), interpolation=cv2.INTER_AREA)
            frames.append(rgb)
            if len(frames) == count:
                break
    if len(frames) != count:
        raise ValueError(f"{path} has {len(frames)} frames, expected {count}")
    return np.stack(frames)


def psnr(a, b, region):
    if not region.any():
        return None
    error = ((a.astype(np.float64) - b.astype(np.float64)) ** 2)[region].mean()
    return 99.0 if error == 0 else float(10 * np.log10(255**2 / error))


def flow(gray_a, gray_b):
    return cv2.calcOpticalFlowFarneback(gray_a, gray_b, None, 0.5, 4, 21, 3, 5, 1.2, 0)


def small_gray(frame):
    return cv2.cvtColor(
        cv2.resize(frame, (FLOW_SIZE, FLOW_SIZE), interpolation=cv2.INTER_AREA),
        cv2.COLOR_RGB2GRAY,
    )


def measure(driver, candidate, masks):
    """driver, candidate: [N, H, W, 3] uint8 with equal N; masks: [N, H, W] bool of the subject."""
    count = len(driver)
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * BACKGROUND_MARGIN_PX + 1, 2 * BACKGROUND_MARGIN_PX + 1)
    )
    background_psnr, subject_cosine = [], []
    driver_energy, candidate_energy, global_cosine = [], [], []
    endpoint_error_sum, driver_magnitude_sum = 0.0, 0.0
    grown = [cv2.dilate(m.astype(np.uint8), kernel).astype(bool) for m in masks]
    for index in range(count):
        background_psnr.append(psnr(driver[index], candidate[index], ~grown[index]))
        if index == count - 1:
            break
        a, b = small_gray(driver[index]), small_gray(driver[index + 1])
        c, d = small_gray(candidate[index]), small_gray(candidate[index + 1])
        fd, fc = flow(a, b), flow(c, d)
        region = cv2.resize(
            grown[index].astype(np.uint8), (FLOW_SIZE, FLOW_SIZE), interpolation=cv2.INTER_NEAREST
        ).astype(bool)
        magnitude_d = np.linalg.norm(fd, axis=2)
        magnitude_c = np.linalg.norm(fc, axis=2)
        driver_energy.append(float(magnitude_d[region].mean()) if region.any() else 0.0)
        candidate_energy.append(float(magnitude_c[region].mean()) if region.any() else 0.0)
        dot = (fd * fc).sum(axis=2)
        cosine = dot / np.maximum(magnitude_d * magnitude_c, 1e-6)
        weight = magnitude_d * region
        subject_cosine.append(
            float((cosine * weight).sum() / weight.sum()) if weight.sum() > 1e-3 else None
        )
        if region.any():
            endpoint_error_sum += float(np.linalg.norm(fd - fc, axis=2)[region].sum())
            driver_magnitude_sum += float(magnitude_d[region].sum())
        all_weight = magnitude_d
        global_cosine.append(
            float((cosine * all_weight).sum() / all_weight.sum())
            if all_weight.sum() > 1e-3
            else None
        )

    def mean(values):
        values = [v for v in values if v is not None]
        return round(float(np.mean(values)), 3) if values else None

    timing = (
        float(np.corrcoef(driver_energy, candidate_energy)[0, 1])
        if np.std(driver_energy) > 1e-6 and np.std(candidate_energy) > 1e-6
        else None
    )
    return {
        "background_psnr_db": mean(background_psnr),
        "background_psnr_min_db": round(min(v for v in background_psnr if v is not None), 2),
        "subject_flow_cosine": mean(subject_cosine),
        "subject_flow_error": round(endpoint_error_sum / max(driver_magnitude_sum, 1e-6), 3),
        "global_flow_cosine": mean(global_cosine),
        "motion_timing_r": round(timing, 3) if timing is not None else None,
        "subject_energy_ratio": round(
            float(np.mean(candidate_energy) / max(np.mean(driver_energy), 1e-6)), 3
        ),
        "per_frame": {
            "background_psnr_db": [round(v, 2) if v is not None else None for v in background_psnr],
            "driver_energy": [round(v, 3) for v in driver_energy],
            "candidate_energy": [round(v, 3) for v in candidate_energy],
            "subject_flow_cosine": [round(v, 3) if v is not None else None for v in subject_cosine],
        },
    }


GLOSSARY = {
    "background_psnr_db": (
        "PSNR of the pixels outside the tracked subject (plus a margin) against the driving "
        "clip; high means the scene and camera survived. Re-encoding alone gives about 35 to 45."
    ),
    "subject_flow_cosine": (
        "Direction agreement of optical flow inside the subject region, weighted by the driving "
        "flow; 1 means the subject moves the same way at the same time, 0 is unrelated."
    ),
    "subject_flow_error": (
        "Mean flow endpoint error inside the subject region divided by the driving flow "
        "magnitude; 0 is identical motion, 1 is as wrong as standing still."
    ),
    "global_flow_cosine": "Flow direction agreement over the whole frame, weighted by driving flow",
    "motion_timing_r": (
        "Correlation of the per-frame subject motion energy curves; 1 means the beats land at "
        "the same moments."
    ),
    "subject_energy_ratio": (
        "Candidate subject motion energy divided by the driving clip's; below 1 is calmer, "
        "above 1 is busier."
    ),
}
