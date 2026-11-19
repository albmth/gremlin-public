"""Pure selection from a fresh, CUDA-filtered RunPod SERVERLESS catalog.

Profiles qualify exact models and images, never entire mutable provider GPU pools.
The caller owns discovery, reservations and endpoint changes; no network calls occur here.
"""

from __future__ import annotations

import json
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path

from gremlin_control.model_release import configured_release, model_references

TIERS = ("complex-motion", "simple-motion")
RESERVATION_SECONDS = 3060
MONTH_SECONDS = 30 * 24 * 3600


def model_frames(duration_ms):
    frames = ceiling(Decimal(duration_ms) * 16 / 1000)
    return ceiling(Decimal(frames - 1) / 4) * 4 + 1


def workload_seconds(profile, job):
    """Scale measured whole-job time by padded model frames, never spending authority."""
    sample = profile.get("workload_measurements", {}).get(job["tier"])
    if not sample:
        return Decimal(str(profile["tier_seconds"][job["tier"]]))
    # Keep fixed preparation/encoding costs when estimating a shorter clip. Non-square
    # canvases process more pixels; no benchmark justifies a cheaper-than-linear estimate.
    frame_ratio = Decimal(model_frames(job["duration_ms"])) / sample["model_frames"]
    pixel_ratio = Decimal(job.get("model_pixels", 512 * 896)) / (512 * 512)
    fixed = Decimal(str(sample["fixed_seconds"]))
    inference = Decimal(str(sample["generation_seconds"])) * frame_ratio
    return (fixed + inference) * pixel_ratio * Decimal("1.10")


def load_qualifications():
    return json.loads(Path(__file__).with_name("gpu_qualifications.json").read_text())


def ceiling(value):
    return int(Decimal(str(value)).to_integral_value(rounding=ROUND_CEILING))


def positive(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number > 0 else None
    except (InvalidOperation, ValueError):
        return None


def cuda_version(value):
    try:
        parts = tuple(int(part) for part in str(value).split("."))
        return parts if len(parts) == 2 and all(part >= 0 for part in parts) else (0, 0)
    except ValueError:
        return (0, 0)


def _patch(candidates, catalog, minimum_cuda, placement, avoided):
    pools = sorted({candidate["pool"] for candidate in candidates})
    selected = {candidate["gpu_id"] for candidate in candidates}
    return {
        "gpu": {
            "pools": pools,
            "excludedTypes": sorted(
                gpu["id"]
                for gpu in catalog
                if gpu.get("pool") in pools and gpu.get("id") not in selected
            ),
            "count": 1,
            "minCudaVersion": minimum_cuda,
            "allowedCudaVersions": [],
        },
        # Every data centre of the placement regions, not the ones showing stock right now.
        # RunPod records a new endpoint release for each change of this list and then
        # replaces the retained worker's container, so the list must not follow stock
        # readings between dispatches (nine of nine re-created containers on 27 to 29
        # October 2026 followed such a change). A placement that failed is left out only
        # while every selected GPU avoids it.
        "dataCenterIds": [
            dc for dc in placement if not all((gpu_id, dc) in avoided for gpu_id in selected)
        ],
    }


def select_gpus(
    catalog,
    endpoint,
    volumes,
    *,
    tiers=TIERS,
    hourly_cap_microusd,
    budget_microusd,
    data_centers=None,
    qualifications=None,
    allow_trial=False,
    avoid_placements=(),
    qualification_target_gpu_id="",
    workload=None,
    warm_gpu_ids=(),
):
    """Return a sanitized audit and an economical native-fallback endpoint patch.

    Budget is remaining authorized headroom, not the lifetime budget. Catalog must have
    been queried with SERVERLESS, SECURE, count=1 and minCudaVersion=13.0. Stock does not
    reserve a worker. Trials require caller-enforced staging authority and never promote
    a profile. Every session may lease either tier, even when estimating one queued tier.
    """
    if qualification_target_gpu_id and not allow_trial:
        raise ValueError("A qualification target requires explicit trial authority")
    profiles = qualifications or load_qualifications()
    if workload is not None and (
        not workload
        or len(workload) > 16
        or any(
            job.get("tier") not in TIERS
            or type(job.get("duration_ms")) is not int
            or not 3000 <= job["duration_ms"] <= 5000
            or job.get("model_pixels", 512 * 896) not in {512 * 512, 512 * 896}
            for job in workload
        )
    ):
        raise ValueError("Invalid queued workload for GPU selection")
    if workload:
        tiers = tuple(dict.fromkeys(job["tier"] for job in workload))
    gpus = catalog.get("gpus", []) if isinstance(catalog, dict) else catalog
    dc_rows = (
        (data_centers or {}).get("dataCenters", [])
        if isinstance(data_centers, dict)
        else data_centers or []
    )
    dc_catalog = {dc["id"]: dc for dc in dc_rows}
    placement = sorted(
        dc_id
        for dc_id, dc in dc_catalog.items()
        if isinstance(dc_id, str) and dc.get("region") in profiles["placement_regions"]
    )
    gpu_profiles = {profile["id"]: profile for profile in profiles["gpus"]}
    avoided = {(item["gpu_id"], item["data_center_id"]) for item in avoid_placements}
    by_volume = {volume["id"]: volume for volume in volumes}
    mounts = endpoint.get("mounts", {})
    volume_ids = endpoint.get("networkVolumes", [])
    volume_ids = sorted(set(volume_ids) | {v["volumeId"] for v in mounts.get("network", [])})
    image = profiles["images"].get(endpoint.get("image"))
    common = []
    try:
        weight_source, model_release = configured_release(endpoint)
    except ValueError:
        weight_source, model_release = None, None
        common.append("model_release_configuration_invalid")
    release = None
    if image and image.get("supports_model_release"):
        release = next(
            (
                entry
                for entry in image.get("releases", [])
                if entry.get("weight_source") == weight_source
                and entry.get("model_release") == model_release
                and entry.get("engine_args") == profiles["engine_args"]
            ),
            None,
        )
        if release is None or weight_source is None:
            common.append("model_release_not_registered")
        if weight_source is not None and endpoint.get("modelReferences") != model_references(
            weight_source, model_release
        ):
            common.append("provider_model_reference_mismatch")
    elif weight_source is not None:
        common.append("worker_model_release_guard_missing")
    storage_mode = (
        "native-cache" if weight_source == "native-cache" else "network" if volume_ids else "local"
    )
    storage = profiles["storage"][storage_mode]
    environment = endpoint.get("env", {})
    if not image or (image["status"] != "accepted" and not allow_trial):
        common.append("worker_image_not_accepted")
    if image and image.get("transport_version") != 2:
        common.append("worker_transport_not_compatible")
    if (
        image
        and not image.get("supports_gpu_constraints")
        and (not allow_trial or qualification_target_gpu_id)
    ):
        common.append("worker_runtime_compatibility_guard_missing")
    if environment.get("IMPRINT_ENGINE_ARGS") != profiles["engine_args"]:
        common.append("engine_args_not_qualified")
    if (
        environment.get("IMPRINT_MODELS_ROOT", "") != storage["models_root"]
        or environment.get("IMPRINT_MODELS_STAGE", "") != storage["models_stage"]
    ):
        common.append("model_storage_configuration_not_qualified")
    if mounts.get("global"):
        common.append("global_volume_not_qualified")
    if weight_source is not None and (
        volume_ids
        or environment.get("IMPRINT_WEIGHTS_BUNDLED")
        or (weight_source == "runtime-hf" and environment.get("HF_HUB_OFFLINE") == "1")
    ):
        common.append("model_storage_configuration_not_qualified")
    disk_gb = positive(endpoint.get("disk"))
    if disk_gb is None or disk_gb < profiles["minimum_disk_gb"]:
        common.append("insufficient_container_disk")
    if endpoint.get("gpu", {}).get("count") != 1:
        common.append("gpu_count_not_qualified")
    if set(tiers) - set(TIERS) or not tiers:
        common.append("unknown_workload_tier")
    if not placement:
        # An empty list would hand placement to the provider's default, which is worldwide.
        common.append("placement_data_centers_unresolved")
    if any(volume_id not in by_volume for volume_id in volume_ids):
        common.append("network_volume_metadata_missing")
    attached = [by_volume[volume_id] for volume_id in volume_ids if volume_id in by_volume]
    volume_dcs = {volume["dataCenter"] for volume in attached}
    if len(volume_dcs) != len(attached):
        common.append("multiple_volumes_in_one_datacenter")
    if any(volume.get("type") != "STANDARD" for volume in attached):
        common.append("network_volume_storage_rate_unknown")
    idle_seconds = endpoint.get("workers", {}).get("idleTimeout")
    if not isinstance(idle_seconds, int) or not 0 <= idle_seconds <= 30:
        common.append("idle_timeout_not_qualified")
        idle_seconds = 30
    candidates = []
    exclusions = []
    for gpu in gpus:
        reasons = list(common)
        gpu_id = gpu.get("id", "unknown")
        if qualification_target_gpu_id and gpu_id != qualification_target_gpu_id:
            reasons.append("outside_qualification_target")
        profile = gpu_profiles.get(gpu_id)
        if not profile:
            reasons.append("gpu_not_qualified")
        elif (profile["status"] != "accepted" or not profile.get("measured")) and not allow_trial:
            reasons.append("gpu_requires_quality_and_performance_acceptance")
        if image and image.get("supports_model_release"):
            accepted_image = bool(release and gpu_id in release.get("accepted_gpu_ids", []))
            trial_image = bool(release and gpu_id in release.get("trial_gpu_ids", []))
        else:
            # These deployed images predate release handshakes and retain their qualified
            # runtime/volume contracts solely as independently deployable rollback targets.
            accepted_image = profile and endpoint.get("image") in profile.get("accepted_images", [])
            trial_image = profile and endpoint.get("image") in profile.get("trial_images", [])
        if profile and not (accepted_image or (allow_trial and trial_image)):
            reasons.append("gpu_image_combination_not_qualified")
        if profile and profile.get("compute_capability") not in profiles["compute_capabilities"]:
            reasons.append("compute_capability_not_qualified")
        if profile and any(not positive(profile.get("tier_seconds", {}).get(t)) for t in TIERS):
            reasons.append("accepted_tier_performance_missing")
        if gpu.get("manufacturer") != "NVIDIA":
            reasons.append("non_cuda_gpu")
        memory = positive(gpu.get("memory"))
        if memory is None or memory < profiles["minimum_vram_gb"]:
            reasons.append("insufficient_or_unknown_vram")
        if not gpu.get("pool"):
            reasons.append("no_serverless_pool")
        if gpu.get("availability") not in {"LOW", "MEDIUM", "HIGH"}:
            reasons.append("insufficient_or_unknown_serverless_availability")
        rate = positive(gpu.get("price", {}).get("serverless"))
        hourly = ceiling(rate * 1_000_000) if rate else 0
        reservation = ceiling(Decimal(hourly) * RESERVATION_SECONDS / 3600)
        if not rate:
            reasons.append("serverless_price_missing")
        elif hourly > hourly_cap_microusd:
            reasons.append("hourly_authority_exceeded")
        if reservation > budget_microusd or budget_microusd <= 0:
            reasons.append("reservation_exceeds_budget")
        if not any(
            cuda.get("available") is True
            and cuda_version(cuda.get("version")) >= cuda_version(profiles["minimum_cuda"])
            for cuda in gpu.get("cudaVersions", [])
        ):
            reasons.append("compatible_cuda_capacity_missing")
        locations = []
        location_exclusions = []
        for dc in gpu.get("dataCenters", []):
            dc_id = dc.get("id")
            metadata = dc_catalog.get(dc_id)
            location_reasons = []
            if (gpu_id, dc_id) in avoided:
                location_reasons.append("previous_provisioning_failure")
            if dc.get("availability") not in {"LOW", "MEDIUM", "HIGH"}:
                location_reasons.append("insufficient_or_unknown_capacity")
            if volume_ids and dc_id not in volume_dcs:
                location_reasons.append("network_volume_in_another_datacenter")
            if not metadata or not metadata.get("region"):
                location_reasons.append("datacenter_metadata_missing")
            if any(
                volume["type"] not in (metadata or {}).get("networkVolumeTypes", [])
                for volume in attached
                if volume["dataCenter"] == dc_id
            ):
                location_reasons.append("network_volume_type_unavailable")
            if location_reasons:
                location_exclusions.append({"id": dc_id, "reasons": location_reasons})
                continue
            locations.append(
                {"id": dc_id, "region": metadata["region"], "availability": dc["availability"]}
            )
        if not locations:
            reasons.append("no_available_compatible_datacenter")
        if reasons:
            exclusions.append(
                {
                    "gpu_id": gpu_id,
                    "reasons": reasons,
                    "datacenter_exclusions": location_exclusions,
                    "pool": gpu.get("pool"),
                    "memory_gb": gpu.get("memory"),
                    "hourly_microusd": hourly,
                    "availability": gpu.get("availability"),
                    "cuda_versions": [
                        {"version": c.get("version"), "available": c.get("available")}
                        for c in gpu.get("cudaVersions", [])
                    ],
                }
            )
            continue
        cold = Decimal(
            str(
                profile.get("cold_start_seconds", {}).get(
                    storage_mode, storage["cold_start_seconds"]
                )
            )
        )
        warm = gpu_id in warm_gpu_ids
        if warm:
            cold = Decimal(0)
        count = 1
        if workload:
            runtime = Decimal(0)
            count = 0
            for job in workload:
                # The existing lease stops starting attempts with under 15 minutes left.
                if count and cold + runtime > 1500:
                    break
                runtime += workload_seconds(profile, job)
                count += 1
        else:
            runtime = max(Decimal(str(profile["tier_seconds"][tier])) for tier in tiers)
        seconds = cold + runtime + idle_seconds
        compute = ceiling(Decimal(hourly) * seconds / 3600)
        costs = profiles["costs"]
        disk = ceiling(
            disk_gb
            * costs["container_disk_microusd_per_gb_month"]
            * ceiling(seconds / 300)
            * 300
            / MONTH_SECONDS
        )
        transfer = costs["transfer_allowance_microusd_per_job"] * count
        candidate = {
            "gpu_id": gpu_id,
            "pool": gpu["pool"],
            "availability": gpu["availability"],
            "qualification": "accepted"
            if profile["status"] == image["status"] == "accepted" and accepted_image
            else "trial",
            "performance_measured": profile["measured"],
            "compute_capability": profile["compute_capability"],
            "data_centers": locations,
            "datacenter_exclusions": location_exclusions,
            "hourly_microusd": hourly,
            "reservation_microusd": reservation,
            "runtime_seconds": float(runtime),
            "estimated_jobs": count,
            "warm_worker_observed": warm,
            "runtime_estimate_evidence": profile.get(
                "runtime_estimate_evidence", profile["evidence"]
            ),
            "cold_start_seconds": float(cold),
            "startup_estimate_evidence": (
                profile.get("cold_start_evidence", storage["evidence"])
                if storage_mode in profile.get("cold_start_seconds", {})
                else storage["evidence"]
            ),
            "cold_start_microusd": ceiling(Decimal(hourly) * cold / 3600),
            "estimated_compute_microusd": compute,
            "estimated_disk_microusd": disk,
            "transfer_allowance_microusd": transfer,
            "estimated_session_microusd": compute + disk + transfer,
            "estimated_job_microusd": ceiling(Decimal(compute + disk + transfer) / count),
            "evidence": profile["evidence"],
        }
        candidates.append(candidate)
    candidates.sort(key=lambda item: (item["estimated_job_microusd"], item["gpu_id"]))
    # Region fields are provider preferences. Prefer a different exact GPU after a failure;
    # when only another region remains, report its weaker placement guarantee explicitly.
    failed_gpus = {gpu_id for gpu_id, _ in avoided}
    alternate_gpus = [item for item in candidates if item["gpu_id"] not in failed_gpus]
    practical = alternate_gpus or candidates
    # LOW is limited stock, not proof of unavailability. It remains usable when every
    # compatible placement has limited stock and is protected by the same queue timeout.
    stronger_capacity = [
        item
        for item in practical
        if item["availability"] in {"MEDIUM", "HIGH"}
        and any(dc["availability"] in {"MEDIUM", "HIGH"} for dc in item["data_centers"])
    ]
    practical = stronger_capacity or practical
    retry_strategy = (
        "alternate_gpu"
        if avoided and alternate_gpus
        else "preferred_region"
        if avoided and candidates
        else "initial"
    )
    selected = []
    if practical:
        best = practical[0]
        # Native fallbacks have no strict order. Bound their economic premium instead of
        # letting a large historical cold-start estimate admit arbitrarily slower GPUs.
        premium = min(
            ceiling(Decimal(best["cold_start_microusd"]) / best["estimated_jobs"]),
            ceiling(Decimal(best["estimated_job_microusd"]) / 10),
        )
        ceiling_cost = best["estimated_job_microusd"] + premium
        selected = [item for item in practical if item["estimated_job_microusd"] <= ceiling_cost][
            :3
        ]
    return {
        "profile_version": profiles["version"],
        "worker_image": endpoint.get("image"),
        "worker_supports_gpu_constraints": bool(image and image.get("supports_gpu_constraints")),
        "worker_supports_model_release": bool(image and image.get("supports_model_release")),
        "weight_source": weight_source,
        "model_release": model_release,
        "qualification_target_gpu_id": qualification_target_gpu_id,
        "storage_mode": storage_mode,
        "cost_assumptions_checked_at": profiles["costs"]["checked_at"],
        "tiers": list(tiers),
        "workload": workload,
        "success_assumption": "accepted output per attempt; production failure rate unmeasured",
        "avoid_placements": [
            {"gpu_id": gpu_id, "data_center_id": dc_id} for gpu_id, dc_id in sorted(avoided)
        ],
        "retry_strategy": retry_strategy,
        "candidates": candidates,
        "exclusions": exclusions,
        "selected_gpu_ids": [item["gpu_id"] for item in selected],
        "gpu_constraints": [
            {
                "gpu_id": item["gpu_id"],
                "runtime_gpu_names": gpu_profiles[item["gpu_id"]]["runtime_gpu_names"],
                "compute_capability": list(cuda_version(item["compute_capability"])),
                "min_vram_gib": 30,
                "min_host_memory_gib": gpu_profiles[item["gpu_id"]]["minimum_host_memory_gib"],
                "min_cuda_version": profiles["minimum_cuda"],
            }
            for item in selected
        ],
        "placement_data_center_ids": placement,
        "gpu_patch": (
            _patch(selected, gpus, profiles["minimum_cuda"], placement, avoided)
            if selected
            else None
        ),
        "hourly_microusd": max((item["hourly_microusd"] for item in selected), default=0),
        "reservation_microusd": max((item["reservation_microusd"] for item in selected), default=0),
        "estimated_job_microusd": selected[0]["estimated_job_microusd"] if selected else None,
        "standing_volume_monthly_microusd": sum(
            volume["size"] * profiles["costs"]["standard_volume_microusd_per_gb_month"]
            for volume in attached
            if volume.get("type") == "STANDARD"
        ),
        "rationale": (
            "Rank per delivered job using the queued tiers, padded frames, canvas, measured "
            "generation plus fixed time with 10% margin, shared boot/idle and rounded disk. "
            "A compatible ready worker avoids another boot; it does not promise a cache hit. "
            "Native alternatives cost at most 10% more and at most one amortized cold start, "
            "limited to three exact GPU models. RunPod may select any included model; "
            "reserve the highest rate. The endpoint's data-centre list is every data centre "
            "of the configured placement regions, independent of stock, so routine dispatches "
            "change nothing on the endpoint and keep its retained worker. On retry prefer an "
            "untried GPU; a regional retry only "
            "changes preferred datacenters, not a hard location guarantee. Prefer MEDIUM/HIGH "
            "capacity and use LOW when no stronger compatible candidate remains. Standing volume "
            "cost is separate. Trials are provisional. A configured qualification target "
            "restricts explicit guarded trials to that model without bypassing compatibility, "
            "live availability or budget checks."
        ),
    }
