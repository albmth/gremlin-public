---
license: mit
tags:
- comfyui
- diffusion-single-file
base_model:
- zai-org/SCAIL-2
---

# SCAIL-2

Repackaged model files for ComfyUI.

Original model repository: https://huggingface.co/zai-org/SCAIL-2

Place the files in the following folders:

```
📂 ComfyUI/
├── 📂 models/
│   ├── 📂 diffusion_models/
│   │   ├── wan2.1_14B_SCAIL_2_fp16.safetensors
│   │   ├── wan2.1_14B_SCAIL_2_fp8_scaled.safetensors
│   │   ├── wan2.1_14B_SCAIL_2_int8_convrot.safetensors
│   │   ├── wan2.1_14B_SCAIL_2_mxfp8.safetensors
│   │   └── wan2.1_14B_SCAIL_2_nvfp4_mxpf8_mix.safetensors
│   ├── 📂 loras/
│   │   ├── wan2.1_SCAIL_2_DPO_lora_bf16.safetensors
│   │   └── wan2.1_SCAIL_2_relight_lora_bf16.safetensors
```

## Workflows

<table>
<thead>
<tr><th align="center" valign="middle" style="text-align:center;vertical-align:middle">Workflow</th><th colspan="2" align="center" valign="middle" style="text-align:center;vertical-align:middle">Thumb</th></tr>
</thead>
<tbody>
<tr><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_wan21_scail2_character_replacement.json">SCAIL-2: Character Replacement</a></td><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_wan21_scail2_character_replacement.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/video_wan21_scail2_character_replacement-2.webp" width="200" height="200" alt="SCAIL-2: Character Replacement"></a><div align="center">Before</div></td><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_wan21_scail2_character_replacement.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/video_wan21_scail2_character_replacement-1.webp" width="200" height="200" alt="SCAIL-2: Character Replacement"></a><div align="center">After</div></td></tr>
<tr><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_wan21_scail2_character_replacement_int8.json">SCAIL-2 Int8: Character Replacement</a></td><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_wan21_scail2_character_replacement_int8.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/video_wan21_scail2_character_replacement_int8-2.webp" width="200" height="200" alt="SCAIL-2 Int8: Character Replacement"></a><div align="center">Before</div></td><td align="center" valign="middle" style="text-align:center;vertical-align:middle"><a href="https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_wan21_scail2_character_replacement_int8.json"><img src="https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/templates/video_wan21_scail2_character_replacement_int8-1.webp" width="200" height="200" alt="SCAIL-2 Int8: Character Replacement"></a><div align="center">After</div></td></tr>
</tbody>
</table>
