"""ComfyUI local image generation backend.

Runs Juggernaut XL (SDXL) on the local RTX 3060 via the ComfyUI REST API.
ComfyUI is installed at /home/eeros/ComfyUI and is auto-started when needed.

Architecture:
- Starts ComfyUI server if not already running (background process)
- Submits SDXL txt2img workflow via /api/prompt
- Polls /api/history until the job completes
- Copies the output PNG to ~/.hermes/cache/images/ and returns the path
"""

from __future__ import annotations

import copy
import json
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    success_response,
)

logger = logging.getLogger(__name__)

COMFYUI_DIR = Path("/home/eeros/ComfyUI")
COMFYUI_VENV = COMFYUI_DIR / "venv" / "bin" / "python"
COMFYUI_HOST = "127.0.0.1"
COMFYUI_PORT = 8188
COMFYUI_URL = f"http://{COMFYUI_HOST}:{COMFYUI_PORT}"

CHECKPOINT_NAME = "Juggernaut-XL_v9.safetensors"

# SDXL resolutions — native training resolution is 1024x1024
ASPECT_RATIO_SIZES = {
    "landscape": (1216, 832),
    "square": (1024, 1024),
    "portrait": (832, 1216),
}

# Base SDXL txt2img workflow (API format)
BASE_WORKFLOW: Dict[str, Any] = {
    "4": {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": CHECKPOINT_NAME},
    },
    "5": {
        "class_type": "EmptyLatentImage",
        "inputs": {"width": 1024, "height": 1024, "batch_size": 1},
    },
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {
            "text": "",
            "clip": ["4", 1],
        },
    },
    "7": {
        "class_type": "CLIPTextEncode",
        "inputs": {
            "text": (
                "watermark, text, logo, blurry, low quality, "
                "deformed, ugly, bad anatomy, worst quality"
            ),
            "clip": ["4", 1],
        },
    },
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 0,
            "steps": 30,
            "cfg": 7.0,
            "sampler_name": "dpmpp_2m",
            "scheduler": "karras",
            "denoise": 1.0,
            "model": ["4", 0],
            "positive": ["6", 0],
            "negative": ["7", 0],
            "latent_image": ["5", 0],
        },
    },
    "8": {
        "class_type": "VAEDecode",
        "inputs": {
            "samples": ["3", 0],
            "vae": ["4", 2],
        },
    },
    "9": {
        "class_type": "SaveImage",
        "inputs": {
            "filename_prefix": "hermes_gen",
            "images": ["8", 0],
        },
    },
}


def _server_is_running() -> bool:
    """Check if ComfyUI server is up and responding."""
    try:
        import urllib.request
        with urllib.request.urlopen(f"{COMFYUI_URL}/system_stats", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _start_server() -> bool:
    """Start ComfyUI in the background. Returns True when server is ready."""
    if _server_is_running():
        return True

    logger.info("Starting ComfyUI server at %s...", COMFYUI_DIR)
    log_path = Path("/tmp/comfyui_hermes.log")

    with open(log_path, "w") as log_f:
        subprocess.Popen(
            [
                str(COMFYUI_VENV),
                str(COMFYUI_DIR / "main.py"),
                "--listen", COMFYUI_HOST,
                "--port", str(COMFYUI_PORT),
                "--output-directory", str(COMFYUI_DIR / "output"),
                "--disable-auto-launch",
            ],
            cwd=str(COMFYUI_DIR),
            stdout=log_f,
            stderr=log_f,
            start_new_session=True,
        )

    # Wait up to 90 seconds for server to become ready
    for i in range(90):
        time.sleep(1)
        if _server_is_running():
            logger.info("ComfyUI server ready after %ds", i + 1)
            return True
        if i % 10 == 9:
            logger.info("Still waiting for ComfyUI... (%ds)", i + 1)

    logger.error("ComfyUI server did not start within 90 seconds")
    return False


def _submit_prompt(workflow: Dict[str, Any], client_id: str) -> Optional[str]:
    """Submit a workflow to ComfyUI and return the prompt_id."""
    import urllib.request
    payload = json.dumps({"prompt": workflow, "client_id": client_id}).encode()
    req = urllib.request.Request(
        f"{COMFYUI_URL}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
            return data.get("prompt_id")
    except Exception as exc:
        logger.error("Failed to submit prompt: %s", exc)
        return None


def _wait_for_output(prompt_id: str, timeout: int = 300) -> Optional[str]:
    """Poll /history until the job is done. Returns the output image path or None."""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"{COMFYUI_URL}/history/{prompt_id}", timeout=10
            ) as r:
                history = json.loads(r.read())
                if prompt_id in history:
                    outputs = history[prompt_id].get("outputs", {})
                    for node_data in outputs.values():
                        images = node_data.get("images", [])
                        if images:
                            img = images[0]
                            filename = img["filename"]
                            subfolder = img.get("subfolder", "")
                            img_type = img.get("type", "output")
                            return _fetch_image(filename, subfolder, img_type)
        except Exception as exc:
            logger.debug("Polling error: %s", exc)
        time.sleep(2)
    return None


def _fetch_image(filename: str, subfolder: str, img_type: str) -> Optional[str]:
    """Download the generated image from ComfyUI and save to hermes cache."""
    import urllib.request
    import urllib.parse

    params = urllib.parse.urlencode(
        {"filename": filename, "subfolder": subfolder, "type": img_type}
    )
    url = f"{COMFYUI_URL}/view?{params}"

    cache_dir = Path.home() / ".hermes" / "cache" / "images"
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_path = cache_dir / f"comfyui_{uuid.uuid4().hex[:8]}_{filename}"

    try:
        urllib.request.urlretrieve(url, str(out_path))
        logger.info("Image saved to %s", out_path)
        return str(out_path)
    except Exception as exc:
        logger.error("Failed to fetch image: %s", exc)
        return None


class ComfyUIImageGenProvider(ImageGenProvider):
    """Local ComfyUI backend — SDXL/Juggernaut XL on RTX 3060."""

    @property
    def name(self) -> str:
        return "comfyui"

    @property
    def display_name(self) -> str:
        return "ComfyUI (local RTX 3060)"

    def is_available(self) -> bool:
        return COMFYUI_VENV.exists() and (
            COMFYUI_DIR / "models" / "checkpoints" / CHECKPOINT_NAME
        ).exists()

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": "juggernaut-xl-v9",
                "display": "Juggernaut XL v9",
                "speed": "~30-60s",
                "strengths": "Photorealism, portraits, landscapes",
                "price": "free (local GPU)",
            }
        ]

    def default_model(self) -> Optional[str]:
        return "juggernaut-xl-v9"

    def capabilities(self) -> Dict[str, Any]:
        return {
            "modalities": ["text"],
            "max_reference_images": 0,
        }

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "ComfyUI (local)",
            "badge": "free",
            "tag": "SDXL photorealism on local RTX 3060 — no API key needed",
            "env_vars": [],
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        if not self.is_available():
            return error_response(
                error=(
                    f"ComfyUI or model not found. "
                    f"Expected: {COMFYUI_VENV} and "
                    f"{COMFYUI_DIR}/models/checkpoints/{CHECKPOINT_NAME}"
                ),
                provider=self.name,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
            )

        if not _start_server():
            return error_response(
                error="ComfyUI server failed to start. Check /tmp/comfyui_hermes.log",
                provider=self.name,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
            )

        # Build workflow
        width, height = ASPECT_RATIO_SIZES.get(aspect_ratio, (1024, 1024))
        seed = kwargs.get("seed") or int(uuid.uuid4().int % (2**32))
        steps = kwargs.get("steps", 30)

        workflow = copy.deepcopy(BASE_WORKFLOW)
        workflow["5"]["inputs"]["width"] = width
        workflow["5"]["inputs"]["height"] = height
        workflow["6"]["inputs"]["text"] = prompt
        workflow["3"]["inputs"]["seed"] = seed
        workflow["3"]["inputs"]["steps"] = steps

        client_id = str(uuid.uuid4())
        logger.info(
            "Submitting ComfyUI job: %s @ %dx%d, steps=%d, seed=%d",
            prompt[:60], width, height, steps, seed,
        )

        prompt_id = _submit_prompt(workflow, client_id)
        if not prompt_id:
            return error_response(
                error="Failed to submit prompt to ComfyUI",
                provider=self.name,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
            )

        logger.info("Waiting for ComfyUI job %s...", prompt_id)
        image_path = _wait_for_output(prompt_id, timeout=300)

        if not image_path:
            return error_response(
                error=f"ComfyUI job {prompt_id} timed out or produced no output",
                provider=self.name,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
            )

        return success_response(
            image=image_path,
            model=CHECKPOINT_NAME,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            provider=self.display_name,
            modality="text",
            extra={"seed": seed, "steps": steps, "size": f"{width}x{height}"},
        )


def register(ctx: Any) -> None:
    ctx.register_image_gen_provider(ComfyUIImageGenProvider())
