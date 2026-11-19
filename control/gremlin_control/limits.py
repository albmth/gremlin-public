"""Contract constants shared by the API, the preflight and the published OpenAPI document.

This module imports nothing from the service so that tests outside `control/` can read it.
"""

CONTRACT_VERSION = 3
# The owner-accepted inference recipes under worker/tiers/, chosen explicitly per job.
TIERS = ("simple-motion", "complex-motion")
STATES = (
    "uploading",
    "queued",
    "preparing",
    "running",
    "finishing",
    "succeeded",
    "failed",
    "cancelled",
    "expired",
)
ACTIVE = ("uploading", "queued", "preparing", "running", "finishing")
RUNNING = ("preparing", "running", "finishing")
TERMINAL = ("succeeded", "failed", "cancelled", "expired")
OUTCOMES = ("ready", "failed", "cancelled", "expired")
FAILURE_CODES = (
    "input_rejected",
    "content_rejected",
    "moderation_unavailable",
    "worker_attempt_failed",
    "lease_expired",
    "session_ended",
)
# GPU-side checks the CPU preflight cannot make; each is a property of the job's inputs.
INPUT_REJECTION_REASONS = ("reference_subject_not_found", "target_not_tracked", "driving_scene_cut")
ADMISSION_REASONS = (
    "spending_unauthorized",
    "endpoint_unconfigured",
    "daily_limit_reached",
    "spending_limit_reached",
    "dispatch_disabled",
    "dispatch_held",
    "budget_exhausted",
)
PREFLIGHT_REASONS = (
    "reference_undecodable",
    "reference_not_still",
    "reference_too_large",
    "driving_undecodable",
    "driving_resolution",
    "driving_frame_rate",
    "driving_too_short",
    "driving_timestamps",
)

DURATION_MS = (3000, 5000)
START_MS = (0, 60_000)
SEED_MAX = 2**32 - 1
PROMPT_CHARS = (20, 2000)
IDEMPOTENCY_KEY_CHARS = (16, 100)
OWNER_CHARS = (1, 100)
INPUT_LIMITS = {"reference": 10 * 1024**2, "driving": 30 * 1024**2}
RESULT_LIMITS = {
    "result.mp4": 30 * 1024**2,
    "result.webp": 8 * 1024**2,
    "poster.jpg": 1024**2,
    "diagnostics.zip": 32 * 1024,
}
CONTENT_TYPES = {
    "result.mp4": "video/mp4",
    "result.webp": "image/webp",
    "poster.jpg": "image/jpeg",
    "diagnostics.zip": "application/zip",
}
RESULT_NAMES = ("result.mp4", "result.webp", "poster.jpg")
REFERENCE_MAX_PIXELS = 2 * 1920 * 1080
DRIVING_MAX_SIDE = 1920
DRIVING_MAX_PIXELS = 1920 * 1080
DRIVING_MAX_FPS = 60
DRIVING_FORMATS = ("gif", "mp4", "mov", "mkv", "webm", "webp")
OUTPUT_FPS = 16
STICKER_SIDE = 512
POSTER_MAX_SIDE = 384

ADMISSION_SECONDS = 86400
MEDIA_SECONDS = 72 * 3600
SAVED_SECONDS = 24 * 3600
DIAGNOSTIC_SECONDS = 7 * 86400
RECORD_SECONDS = 30 * 86400
UPLOAD_SECONDS = 300
DOWNLOAD_SECONDS = 120
