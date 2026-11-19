"""Native ComfyUI graphs, split at tracking so masks are checked before diffusion."""

import json

from .recipe import load

SAM = "sam3.1_multiplex_fp16.safetensors"
TEXT = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
VAE = "wan_2.1_vae.safetensors"
VISION = "clip_vision_h.safetensors"


def node(kind, **inputs):
    return {"class_type": kind, "inputs": inputs}


def tracking_graph(prefix, ref_point, target_point):
    return {
        "sam": node("UNETLoader", unet_name=SAM, weight_dtype="default"),
        "ref": node("LoadImage", image=f"{prefix}-reference.png"),
        "video": node("LoadVideo", file=f"{prefix}-driving.mkv"),
        "frames": node("GetVideoComponents", video=["video", 0]),
        "first": node("ImageFromBatch", image=["frames", 0], batch_index=0, length=1),
        "ref_detect": node(
            "SAM3_Detect",
            model=["sam", 0],
            image=["ref", 0],
            positive_coords=json.dumps([ref_point]),
            threshold=0.5,
            refine_iterations=2,
            individual_masks=False,
        ),
        "target_detect": node(
            "SAM3_Detect",
            model=["sam", 0],
            image=["first", 0],
            positive_coords=json.dumps([target_point]),
            threshold=0.5,
            refine_iterations=2,
            individual_masks=False,
        ),
        "track": node(
            "SAM3_VideoTrack",
            images=["frames", 0],
            model=["sam", 0],
            initial_mask=["target_detect", 0],
            detection_threshold=0.5,
            max_objects=1,
            detect_interval=1,
        ),
        "colors": node(
            "SCAIL2ColoredMask",
            driving_track_data=["track", 0],
            ref_track_data=["ref_detect", 0],
            object_indices="0",
            sort_by="none",
            replacement_mode=True,
        ),
        "save_drive_masks": node(
            "SaveImage", images=["colors", 0], filename_prefix=f"{prefix}/drive-mask"
        ),
        "save_ref_mask": node(
            "SaveImage", images=["colors", 1], filename_prefix=f"{prefix}/ref-mask"
        ),
    }


def generation_graph(prefix, width, height, length, seed, prompt, recipe=None):
    """The replacement graph for a recipe; the reference recipe is the accepted one."""
    recipe = recipe or load()
    graph = {
        "unet": node(
            "UNETLoader",
            unet_name=recipe["diffusion"],
            weight_dtype=recipe.get("weight_dtype", "default"),
        )
    }
    model = ["unet", 0]
    for index, lora in enumerate(recipe["loras"]):
        name = f"lora{index}"
        graph[name] = node(
            "LoraLoaderModelOnly",
            model=model,
            lora_name=lora["name"],
            strength_model=float(lora["strength"]),
        )
        model = [name, 0]
    if recipe.get("attention"):
        graph["attention"] = node(
            "ModelAttentionBackend", model=model, attention=recipe["attention"]
        )
        model = ["attention", 0]
    if recipe.get("easycache"):
        cache = recipe["easycache"]
        graph["easycache"] = node(
            "EasyCache",
            model=model,
            reuse_threshold=float(cache.get("reuse_threshold", 0.2)),
            start_percent=float(cache.get("start_percent", 0.15)),
            end_percent=float(cache.get("end_percent", 0.95)),
            verbose=True,
        )
        model = ["easycache", 0]
    graph["shift"] = node("ModelSamplingSD3", model=model, shift=float(recipe["shift"]))
    graph.update(
        {
            # Default placement lets the engine offload the encoder before diffusion; pinning
            # it to CPU spent minutes of an idle GPU encoding two prompts on the first H100 run.
            "text": node("CLIPLoader", clip_name=TEXT, type="wan"),
            "positive": node("CLIPTextEncode", clip=["text", 0], text=prompt),
            "negative": node("CLIPTextEncode", clip=["text", 0], text=""),
            "vae": node("VAELoader", vae_name=VAE),
            "vision": node("CLIPVisionLoader", clip_name=VISION),
            "ref": node("LoadImage", image=f"{prefix}-masked-reference.png"),
            "ref_mask": node("LoadImage", image=f"{prefix}-reference-mask.png"),
            "vision_encode": node(
                "CLIPVisionEncode", clip_vision=["vision", 0], image=["ref", 0], crop="none"
            ),
            "video": node("LoadVideo", file=f"{prefix}-driving.mkv"),
            "frames": node("GetVideoComponents", video=["video", 0]),
            "mask_video": node("LoadVideo", file=f"{prefix}-masks.mkv"),
            "masks": node("GetVideoComponents", video=["mask_video", 0]),
            "condition": node(
                "WanSCAILToVideo",
                positive=["positive", 0],
                negative=["negative", 0],
                vae=["vae", 0],
                width=width,
                height=height,
                length=length,
                batch_size=1,
                pose_video=["frames", 0],
                pose_video_mask=["masks", 0],
                replacement_mode=True,
                pose_strength=1.0,
                pose_start=0.0,
                pose_end=1.0,
                reference_image=["ref", 0],
                reference_image_mask=["ref_mask", 0],
                clip_vision_output=["vision_encode", 0],
                video_frame_offset=0,
                previous_frame_count=5,
            ),
            "sample": node(
                "KSampler",
                model=["shift", 0],
                positive=["condition", 0],
                negative=["condition", 1],
                latent_image=["condition", 2],
                seed=seed,
                steps=int(recipe["steps"]),
                cfg=float(recipe["cfg"]),
                sampler_name=recipe["sampler"],
                scheduler=recipe["scheduler"],
                denoise=1.0,
            ),
        }
    )
    if recipe.get("decode", "tiled") == "tiled":
        graph["decode"] = node(
            "VAEDecodeTiled",
            samples=["sample", 0],
            vae=["vae", 0],
            tile_size=512,
            overlap=64,
            temporal_size=64,
            temporal_overlap=8,
        )
    else:
        graph["decode"] = node("VAEDecode", samples=["sample", 0], vae=["vae", 0])
    graph["save"] = node("SaveImage", images=["decode", 0], filename_prefix=f"{prefix}/generated")
    return graph


def validate_graph(graph, schemas):
    """Check the installed engine's actual socket contracts without loading weights."""
    for name, item in graph.items():
        schema = schemas[item["class_type"]]
        inputs = item["inputs"]
        required = schema["input"].get("required", {})
        available = {**required, **schema["input"].get("optional", {})}
        missing = set(required) - inputs.keys()
        unknown = inputs.keys() - available.keys()
        if missing or unknown:
            raise ValueError(f"{name}: missing={missing}, unknown={unknown}")
        for key, value in inputs.items():
            expected = available[key][0]
            if isinstance(value, list) and len(value) == 2 and value[0] in graph:
                source, slot = value
                actual = schemas[graph[source]["class_type"]]["output"][slot]
                if expected != actual and actual not in str(expected).split(","):
                    raise ValueError(f"{name}.{key}: expected {expected}, got {actual}")
            elif isinstance(expected, list) and value not in expected:
                # File dropdowns are runtime inventories; their bytes are verified by assets.py.
                if key not in {
                    "unet_name",
                    "clip_name",
                    "vae_name",
                    "lora_name",
                    "image",
                    "file",
                }:
                    raise ValueError(f"{name}.{key}: unsupported value {value}")
