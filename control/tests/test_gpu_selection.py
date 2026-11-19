import json
from copy import deepcopy

import pytest
from gremlin_control.gpu_selection import load_qualifications, select_gpus


@pytest.fixture
def inputs():
    profiles = load_qualifications()
    accepted_image = next(
        key for key, value in profiles["images"].items() if value["status"] == "accepted"
    )
    profiles["images"][accepted_image].update(transport_version=2, supports_gpu_constraints=True)
    # Synthetic guarded image: selection tests cannot qualify a deployed worker image.
    profiles["gpus"][1].update(status="accepted", measured=True)
    profiles["gpus"][1]["accepted_images"] = [accepted_image]
    profiles["gpus"][1]["tier_seconds"] = {"complex-motion": 300, "simple-motion": 100}
    # Synthetic workload: the scenarios assume the two GPUs cost within 10% of each other per
    # complex job, which the live registry's measurements no longer guarantee as tiers change.
    for profile, fixed, generation in zip(
        profiles["gpus"], (9.011794, 17.24235), (92.4191, 168.41255), strict=True
    ):
        profile["workload_measurements"]["complex-motion"] = {
            "model_frames": 57,
            "fixed_seconds": fixed,
            "generation_seconds": generation,
        }
    gpus = []
    for profile, rate, pool in zip(
        profiles["gpus"], (1.58, 1.15), ("ADA_32_PRO", "BLACKWELL_32"), strict=True
    ):
        gpus.append(
            {
                "id": profile["id"],
                "manufacturer": "NVIDIA",
                "memory": 32,
                "pool": pool,
                "price": {"serverless": rate, "secure": 0.01},
                "availability": "HIGH",
                "cudaVersions": [{"version": "13.2", "available": True}],
                "dataCenters": [{"id": "EU-RO-1", "availability": "HIGH"}],
            }
        )
    return {
        "catalog": {"gpus": gpus},
        "endpoint": {
            "image": accepted_image,
            "env": {
                "IMPRINT_ENGINE_ARGS": "--fast-disk",
                "IMPRINT_MODELS_ROOT": "/opt/ComfyUI/models",
                "IMPRINT_MODELS_STAGE": "",
            },
            "gpu": {"count": 1},
            "disk": 50,
            "workers": {"idleTimeout": 5},
            "mounts": {"network": [], "global": []},
            "networkVolumes": [],
            "dataCenterIds": [],
        },
        "volumes": [],
        "data_centers": {
            "dataCenters": [
                {"id": "EU-RO-1", "region": "EUROPE", "networkVolumeTypes": ["STANDARD"]},
                {"id": "EUR-NO-1", "region": "EUROPE", "networkVolumeTypes": ["STANDARD"]},
            ]
        },
        "hourly_cap_microusd": 2_000_000,
        "budget_microusd": 2_000_000,
        "qualifications": profiles,
    }


def explicit_release(inputs, source="native-cache"):
    """A synthetic release, never a registration or qualification of a deployed image."""
    image = "ghcr.io/example/worker@sha256:" + "a" * 64
    release = {
        "version": 1,
        "asset_set_sha256": "1" * 64,
        "repo": "example/gremlin-weights" if source == "native-cache" else None,
        "revision": "2" * 40 if source == "native-cache" else None,
    }
    inputs["endpoint"]["image"] = image
    inputs["endpoint"]["env"].update(
        IMPRINT_WEIGHT_SOURCE=source,
        IMPRINT_MODEL_RELEASE=json.dumps(release),
        IMPRINT_MODELS_ROOT="" if source == "native-cache" else "/opt/ComfyUI/models",
    )
    inputs["endpoint"]["modelReferences"] = (
        [f"https://huggingface.co/{release['repo']}:{release['revision']}"]
        if source == "native-cache"
        else []
    )
    inputs["qualifications"]["images"][image] = {
        "status": "trial",
        "transport_version": 2,
        "supports_gpu_constraints": True,
        "supports_model_release": True,
        "releases": [
            {
                "weight_source": source,
                "model_release": release,
                "engine_args": "--fast-disk",
                "accepted_gpu_ids": [],
                "trial_gpu_ids": [profile["id"] for profile in inputs["qualifications"]["gpus"]],
            }
        ],
    }
    return release


def test_rank_total_job_cost_and_reserve_every_native_fallback_rate(inputs):
    report = select_gpus(**inputs)
    assert report["candidates"][0]["gpu_id"] == "NVIDIA RTX PRO 4500 Blackwell"
    assert len(report["selected_gpu_ids"]) == 2
    assert report["hourly_microusd"] == 1_580_000
    assert report["reservation_microusd"] == 1_343_000
    assert report["gpu_patch"]["gpu"]["allowedCudaVersions"] == []
    assert report["candidates"][0]["estimated_disk_microusd"] > 0
    assert report["candidates"][0]["transfer_allowance_microusd"] > 0
    assert report["worker_supports_gpu_constraints"] is True

    # A much slower card loses despite a lower hourly quote and leaves the native set.
    inputs["qualifications"]["gpus"][1]["tier_seconds"]["complex-motion"] = 1200
    slow = select_gpus(**inputs)
    assert slow["selected_gpu_ids"] == ["NVIDIA GeForce RTX 5090"]
    assert slow["candidates"][1]["hourly_microusd"] < slow["hourly_microusd"]


def test_unavailable_model_does_not_block_an_available_qualified_alternative(inputs):
    # Previous preferred placement is not a permanent regional restriction for local weights.
    inputs["endpoint"]["dataCenterIds"] = ["EUR-NO-1"]
    gpu = inputs["catalog"]["gpus"][0]
    gpu.update(availability="NONE", dataCenters=[])
    report = select_gpus(**inputs)
    assert report["selected_gpu_ids"] == ["NVIDIA RTX PRO 4500 Blackwell"]
    assert report["gpu_patch"]["dataCenterIds"] == ["EU-RO-1", "EUR-NO-1"]
    assert report["gpu_patch"]["gpu"]["pools"] == ["BLACKWELL_32"]


def test_endpoint_patch_lists_every_placement_region_data_center_regardless_of_stock(inputs):
    # The patch must not follow stock readings: each change of the endpoint's list is a
    # provider release that evicts the retained worker's container (27 to 29 October 2026).
    inputs["data_centers"]["dataCenters"] += [
        {"id": "EU-CZ-1", "region": "EUROPE", "networkVolumeTypes": ["STANDARD"]},
        {"id": "US-TX-3", "region": "NORTH_AMERICA", "networkVolumeTypes": ["STANDARD"]},
    ]
    first = select_gpus(**inputs)
    assert first["gpu_patch"]["dataCenterIds"] == ["EU-CZ-1", "EU-RO-1", "EUR-NO-1"]
    assert first["placement_data_center_ids"] == ["EU-CZ-1", "EU-RO-1", "EUR-NO-1"]
    for gpu in inputs["catalog"]["gpus"]:
        gpu["dataCenters"] = [{"id": "EUR-NO-1", "availability": "LOW"}]
    second = select_gpus(**inputs)
    assert second["gpu_patch"] == first["gpu_patch"]
    assert [dc["id"] for dc in second["candidates"][0]["data_centers"]] == ["EUR-NO-1"]
    inputs["qualifications"]["placement_regions"] = ["ANTARCTICA"]
    unresolved = select_gpus(**inputs)
    assert unresolved["gpu_patch"] is None
    assert all(
        "placement_data_centers_unresolved" in row["reasons"] for row in unresolved["exclusions"]
    )


def test_limited_capacity_is_usable_when_stronger_qualified_stock_disappears(inputs):
    first, second = inputs["catalog"]["gpus"]
    second["availability"] = "LOW"
    second["dataCenters"][0]["availability"] = "LOW"
    report = select_gpus(**inputs)
    assert report["candidates"][0]["gpu_id"] == second["id"]
    assert report["selected_gpu_ids"] == [first["id"]]
    first.update(availability="NONE", dataCenters=[])
    report = select_gpus(**inputs)
    assert report["selected_gpu_ids"] == [second["id"]]
    assert report["candidates"][0]["data_centers"][0]["availability"] == "LOW"


def test_volume_pin_and_storage_type_must_match_actual_datacenter(inputs):
    endpoint = inputs["endpoint"]
    endpoint["mounts"]["network"] = [{"volumeId": "weights"}]
    endpoint["env"].update(
        IMPRINT_MODELS_ROOT="/runpod-volume/models", IMPRINT_MODELS_STAGE="/opt/ComfyUI/models"
    )
    inputs["volumes"] = [
        {"id": "weights", "dataCenter": "EUR-NO-1", "type": "STANDARD", "size": 35}
    ]
    report = select_gpus(**inputs)
    assert report["selected_gpu_ids"] == []
    assert (
        "network_volume_in_another_datacenter"
        in (report["exclusions"][0]["datacenter_exclusions"][0]["reasons"])
    )
    inputs["catalog"]["gpus"][1]["dataCenters"] = [{"id": "EUR-NO-1", "availability": "MEDIUM"}]
    report = select_gpus(**inputs)
    assert report["selected_gpu_ids"] == ["NVIDIA RTX PRO 4500 Blackwell"]
    assert report["standing_volume_monthly_microusd"] == 2_450_000
    inputs["data_centers"]["dataCenters"][1]["networkVolumeTypes"] = []
    assert select_gpus(**inputs)["selected_gpu_ids"] == []


def test_new_unqualified_model_in_selected_pool_is_explicitly_excluded(inputs):
    member = deepcopy(inputs["catalog"]["gpus"][1])
    member.update(id="NVIDIA Unreviewed Pool Member", availability="NONE", dataCenters=[])
    inputs["catalog"]["gpus"].append(member)
    report = select_gpus(**inputs)
    assert report["gpu_patch"]["gpu"]["excludedTypes"] == ["NVIDIA Unreviewed Pool Member"]
    assert "gpu_not_qualified" in report["exclusions"][0]["reasons"]


@pytest.mark.parametrize("missing", ["pool", "price", "cudaVersions", "memory", "manufacturer"])
def test_missing_provider_compatibility_data_fails_closed(inputs, missing):
    inputs["catalog"]["gpus"] = inputs["catalog"]["gpus"][:1]
    inputs["catalog"]["gpus"][0].pop(missing)
    report = select_gpus(**inputs)
    assert report["gpu_patch"] is None
    assert report["exclusions"][0]["reasons"]


def test_live_currency_is_rounded_up_and_full_reservation_fits_budget(inputs):
    inputs["catalog"]["gpus"] = inputs["catalog"]["gpus"][1:]
    inputs["catalog"]["gpus"][0]["price"]["serverless"] = "1.1500001"
    report = select_gpus(**inputs)
    assert report["hourly_microusd"] == 1_150_001
    assert report["reservation_microusd"] == 977_501
    inputs["budget_microusd"] = 977_500
    blocked = select_gpus(**inputs)
    assert blocked["gpu_patch"] is None
    assert "reservation_exceeds_budget" in blocked["exclusions"][0]["reasons"]


def test_trial_never_qualifies_gpu_or_changed_worker_configuration(inputs):
    inputs["catalog"]["gpus"] = inputs["catalog"]["gpus"][1:]
    image = inputs["endpoint"]["image"]
    inputs["qualifications"]["images"][image]["status"] = "trial"
    inputs["qualifications"]["gpus"][1].update(
        status="trial", measured=False, accepted_images=[], trial_images=[image]
    )
    assert select_gpus(**inputs)["gpu_patch"] is None
    trial = select_gpus(**inputs, allow_trial=True)
    assert trial["candidates"][0]["qualification"] == "trial"
    assert trial["candidates"][0]["performance_measured"] is False
    assert select_gpus(**inputs)["gpu_patch"] is None
    inputs["endpoint"]["env"]["IMPRINT_ENGINE_ARGS"] = "--disable-dynamic-vram"
    assert select_gpus(**inputs, allow_trial=True)["gpu_patch"] is None


def test_quality_accepted_image_without_runtime_guard_cannot_dispatch(inputs):
    inputs["qualifications"]["images"][inputs["endpoint"]["image"]]["supports_gpu_constraints"] = (
        False
    )
    report = select_gpus(**inputs)
    assert report["gpu_patch"] is None
    assert report["worker_supports_gpu_constraints"] is False
    assert all(
        "worker_runtime_compatibility_guard_missing" in item["reasons"]
        for item in report["exclusions"]
    )


def test_image_and_both_session_tiers_require_qualification(inputs):
    inputs["endpoint"]["image"] = "different-unvalidated-image"
    assert select_gpus(**inputs, allow_trial=True)["gpu_patch"] is None
    inputs["endpoint"]["image"] = next(iter(inputs["qualifications"]["images"]))
    for profile in inputs["qualifications"]["gpus"]:
        del profile["tier_seconds"]["complex-motion"]
    assert select_gpus(**inputs, tiers=["simple-motion"])["gpu_patch"] is None


def test_acceptance_of_one_image_does_not_qualify_other_gpu_image_combinations(inputs):
    inputs["catalog"]["gpus"] = inputs["catalog"]["gpus"][1:]
    alternate = inputs["qualifications"]["gpus"][1]
    alternate["accepted_images"] = alternate["trial_images"]
    report = select_gpus(**inputs)
    assert report["selected_gpu_ids"] == []
    assert "gpu_image_combination_not_qualified" in report["exclusions"][0]["reasons"]


def test_historical_quality_approval_cannot_run_old_transport_on_control_api(inputs):
    inputs["qualifications"] = load_qualifications()
    report = select_gpus(**inputs, allow_trial=True)
    assert report["selected_gpu_ids"] == []
    assert "worker_transport_not_compatible" in report["exclusions"][0]["reasons"]


def test_capacity_retry_prefers_another_gpu_over_reusing_failed_hardware(inputs):
    gpu = inputs["catalog"]["gpus"][0]
    gpu["price"]["serverless"] = 0.50
    gpu["dataCenters"].append({"id": "EUR-NO-1", "availability": "HIGH"})
    report = select_gpus(
        **inputs, avoid_placements=[{"gpu_id": gpu["id"], "data_center_id": "EU-RO-1"}]
    )
    assert report["candidates"][0]["gpu_id"] == gpu["id"]
    assert report["selected_gpu_ids"] == ["NVIDIA RTX PRO 4500 Blackwell"]
    assert report["retry_strategy"] == "alternate_gpu"
    assert report["estimated_job_microusd"] == report["candidates"][1]["estimated_job_microusd"]


def test_regional_retry_excludes_failed_placement_without_claiming_strict_location(inputs):
    inputs["catalog"]["gpus"] = inputs["catalog"]["gpus"][:1]
    gpu = inputs["catalog"]["gpus"][0]
    gpu["dataCenters"].append({"id": "EUR-NO-1", "availability": "HIGH"})
    avoidance = [{"gpu_id": gpu["id"], "data_center_id": "EU-RO-1"}]
    report = select_gpus(**inputs, avoid_placements=avoidance)
    assert report["selected_gpu_ids"] == [gpu["id"]]
    assert report["gpu_patch"]["dataCenterIds"] == ["EUR-NO-1"]
    assert report["retry_strategy"] == "preferred_region"
    assert (
        "previous_provisioning_failure"
        in (report["candidates"][0]["datacenter_exclusions"][0]["reasons"])
    )
    avoidance.append({"gpu_id": gpu["id"], "data_center_id": "EUR-NO-1"})
    assert select_gpus(**inputs, avoid_placements=avoidance)["gpu_patch"] is None


def test_explicit_qualification_targets_the_named_gpu_without_production_pinning(inputs):
    target = inputs["catalog"]["gpus"][0]["id"]
    assert select_gpus(**inputs)["selected_gpu_ids"][0] != target
    with pytest.raises(ValueError, match="explicit trial authority"):
        select_gpus(**inputs, qualification_target_gpu_id=target)
    report = select_gpus(**inputs, allow_trial=True, qualification_target_gpu_id=target)
    assert report["selected_gpu_ids"] == [target]
    assert report["qualification_target_gpu_id"] == target
    assert report["exclusions"][0]["reasons"] == ["outside_qualification_target"]
    assert len(select_gpus(**inputs)["selected_gpu_ids"]) == 2


@pytest.mark.parametrize("constraint", ["capacity", "budget", "image", "guard", "vram", "price"])
def test_qualification_target_cannot_override_deployment_safeguards(inputs, constraint):
    gpu = inputs["catalog"]["gpus"][0]
    if constraint == "capacity":
        gpu.update(availability="NONE", dataCenters=[])
    elif constraint == "budget":
        inputs["budget_microusd"] = 1
    elif constraint == "image":
        inputs["endpoint"]["image"] = "unvalidated-image"
    elif constraint == "guard":
        inputs["qualifications"]["images"][inputs["endpoint"]["image"]][
            "supports_gpu_constraints"
        ] = False
    elif constraint == "vram":
        gpu["memory"] = 16
    elif constraint == "price":
        gpu["price"]["serverless"] = 3
    report = select_gpus(**inputs, allow_trial=True, qualification_target_gpu_id=gpu["id"])
    assert report["selected_gpu_ids"] == []
    assert report["gpu_patch"] is None
    assert next(item for item in report["exclusions"] if item["gpu_id"] == gpu["id"])["reasons"]


def test_measured_queue_uses_frames_canvas_and_amortizes_boot_without_shrinking_reservation(inputs):
    short = {"tier": "complex-motion", "duration_ms": 3400, "model_pixels": 512 * 512}
    single = select_gpus(**inputs, workload=[short])
    batch = select_gpus(**inputs, workload=[short] * 4)
    assert batch["candidates"][0]["gpu_id"] == "NVIDIA GeForce RTX 5090"
    assert batch["candidates"][0]["estimated_jobs"] == 4
    assert batch["estimated_job_microusd"] < single["estimated_job_microusd"]
    assert batch["reservation_microusd"] == 1_343_000

    def by_id(report):
        return {row["gpu_id"]: row for row in report["candidates"]}

    longer = select_gpus(**inputs, workload=[short | {"duration_ms": 5000}])
    wide = select_gpus(**inputs, workload=[short | {"model_pixels": 512 * 896}])
    for gpu_id, row in by_id(single).items():
        assert by_id(longer)[gpu_id]["runtime_seconds"] > row["runtime_seconds"]
        assert by_id(wide)[gpu_id]["runtime_seconds"] > row["runtime_seconds"]


def test_ready_compatible_worker_wins_without_a_new_cold_start_or_lower_spending_cap(inputs):
    job = {"tier": "simple-motion", "duration_ms": 5000, "model_pixels": 512 * 512}
    warm = select_gpus(**inputs, workload=[job], warm_gpu_ids=["NVIDIA GeForce RTX 5090"])
    assert warm["selected_gpu_ids"] == ["NVIDIA GeForce RTX 5090"]
    assert warm["candidates"][0]["cold_start_seconds"] == 0
    assert warm["reservation_microusd"] == 1_343_000
    inputs["budget_microusd"] = 100_000
    assert (
        select_gpus(**inputs, workload=[job], warm_gpu_ids=["NVIDIA GeForce RTX 5090"])["gpu_patch"]
        is None
    )


@pytest.mark.parametrize("source", ["native-cache", "runtime-hf"])
def test_explicit_release_needs_separate_trial_and_preserves_full_reservation(inputs, source):
    release = explicit_release(inputs, source)
    for profile, seconds in zip(inputs["qualifications"]["gpus"], (10, 20), strict=True):
        profile["cold_start_seconds"]["native-cache"] = seconds
    assert select_gpus(**inputs)["gpu_patch"] is None
    report = select_gpus(**inputs, allow_trial=True)
    assert report["model_release"] == release
    assert report["weight_source"] == source
    assert report["reservation_microusd"] == 1_343_000
    assert all(candidate["qualification"] == "trial" for candidate in report["candidates"])
    if source == "native-cache":
        assert {candidate["cold_start_seconds"] for candidate in report["candidates"]} == {10, 20}
    else:
        assert {candidate["cold_start_seconds"] for candidate in report["candidates"]} == {
            88.6,
            69.5,
        }


@pytest.mark.parametrize(
    "change",
    ["revision", "repo", "hash", "reference", "missing", "mode", "stage", "mount", "bundled"],
)
def test_native_release_cannot_dispatch_changed_or_incomplete_configuration(inputs, change):
    release = explicit_release(inputs)
    endpoint = inputs["endpoint"]
    if change in {"revision", "repo", "hash"}:
        release = deepcopy(release)
        release[{"revision": "revision", "repo": "repo", "hash": "asset_set_sha256"}[change]] = {
            "revision": "3" * 40,
            "repo": "different/weights",
            "hash": "3" * 64,
        }[change]
        endpoint["env"]["IMPRINT_MODEL_RELEASE"] = json.dumps(release)
    elif change == "reference":
        endpoint["modelReferences"] = ["https://huggingface.co/example/gremlin-weights:main"]
    elif change == "missing":
        endpoint.pop("modelReferences")
    elif change == "mode":
        endpoint["env"]["IMPRINT_WEIGHT_SOURCE"] = "runtime-hf"
    elif change == "stage":
        endpoint["env"]["IMPRINT_MODELS_STAGE"] = "/opt/ComfyUI/models"
    elif change == "bundled":
        endpoint["env"]["IMPRINT_WEIGHTS_BUNDLED"] = "0"
    else:
        endpoint["mounts"]["network"] = [{"volumeId": "unexpected"}]
    assert select_gpus(**inputs, allow_trial=True)["gpu_patch"] is None


@pytest.mark.parametrize("accepted_source", ["native-cache", "runtime-hf"])
def test_accepted_release_does_not_qualify_other_source_on_same_image(inputs, accepted_source):
    accepted = explicit_release(inputs, accepted_source)
    image = inputs["qualifications"]["images"][inputs["endpoint"]["image"]]
    image["status"] = "accepted"
    accepted_entry = image["releases"][0]
    accepted_entry["accepted_gpu_ids"] = accepted_entry.pop("trial_gpu_ids")
    assert select_gpus(**inputs)["gpu_patch"] is not None

    trial_source = "runtime-hf" if accepted_source == "native-cache" else "native-cache"
    native = trial_source == "native-cache"
    trial_release = dict(
        accepted,
        repo="example/gremlin-weights" if native else None,
        revision="2" * 40 if native else None,
    )
    image["releases"].append(
        dict(
            accepted_entry,
            weight_source=trial_source,
            model_release=trial_release,
            accepted_gpu_ids=[],
            trial_gpu_ids=accepted_entry["accepted_gpu_ids"],
        )
    )
    inputs["endpoint"]["env"].update(
        IMPRINT_WEIGHT_SOURCE=trial_source,
        IMPRINT_MODEL_RELEASE=json.dumps(trial_release),
        IMPRINT_MODELS_ROOT="" if native else "/opt/ComfyUI/models",
    )
    inputs["endpoint"]["modelReferences"] = (
        [f"https://huggingface.co/{trial_release['repo']}:{trial_release['revision']}"]
        if native
        else []
    )
    rejected = select_gpus(**inputs)
    assert rejected["gpu_patch"] is None
    assert all(
        "gpu_image_combination_not_qualified" in row["reasons"] for row in rejected["exclusions"]
    )
    trial = select_gpus(**inputs, allow_trial=True)
    assert trial["gpu_patch"] is not None
    assert all(candidate["qualification"] == "trial" for candidate in trial["candidates"])
