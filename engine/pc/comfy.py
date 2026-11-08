"""A small client for ComfyUI's HTTP API, with the two graphs the engine uses: stills (Z-Image-Turbo) and video (Wan 2.2).

ComfyUI runs headless on 127.0.0.1 only. If it is not up, `Comfy.start()` launches it from the PC folder and `stop()`
ends it, so the GPU is free the rest of the day. Polling /history needs no websocket, so this stays standard library.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

HOST = "http://127.0.0.1:8188"
Z_IMAGE = {"unet": "z_image_turbo_bf16.safetensors", "clip": "qwen_3_4b.safetensors", "vae": "ae.safetensors"}
WAN = {"unet": "wan2.2_ti2v_5B_fp16.safetensors", "clip": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "vae": "wan2.2_vae.safetensors"}
WAN_NEGATIVE = ("static, overexposed, blurry details, subtitles, style, artwork, painting, still image, worst quality, "
                "low quality, JPEG artifacts, ugly, deformed, extra fingers, poorly drawn hands, poorly drawn face, "
                "mutated, disfigured, watermark, text, logo")


def snap(v: int, step: int) -> int:
    return max(step, round(v / step) * step)


def still_graph(prompt: str, width: int, height: int, seed: int, steps: int = 9, prefix: str = "social/still") -> dict:
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": Z_IMAGE["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": Z_IMAGE["clip"], "type": "lumina2"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": Z_IMAGE["vae"]}},
        "4": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3.0}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "6": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["5", 0]}},
        "7": {"class_type": "EmptySD3LatentImage", "inputs": {"width": snap(width, 16), "height": snap(height, 16), "batch_size": 1}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "positive": ["5", 0], "negative": ["6", 0], "latent_image": ["7", 0],
                                                    "seed": seed, "steps": steps, "cfg": 1.0, "sampler_name": "res_multistep",
                                                    "scheduler": "simple", "denoise": 1.0}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveImage", "inputs": {"images": ["9", 0], "filename_prefix": prefix}},
    }


def video_graph(prompt: str, width: int, height: int, frames: int, seed: int, *, fps: int = 24, steps: int = 20, cfg: float = 5.0,
                negative: str = WAN_NEGATIVE, start_image: str | None = None, prefix: str = "social/video") -> dict:
    length = ((max(5, frames) - 1) // 4) * 4 + 1  # Wan wants 4n+1 frames
    g = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": WAN["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": WAN["clip"], "type": "wan"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": WAN["vae"]}},
        "4": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 8.0}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": negative}},
        "7": {"class_type": "Wan22ImageToVideoLatent", "inputs": {"vae": ["3", 0], "width": snap(width, 32), "height": snap(height, 32),
                                                                   "length": length, "batch_size": 1}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "positive": ["5", 0], "negative": ["6", 0], "latent_image": ["7", 0],
                                                    "seed": seed, "steps": steps, "cfg": cfg, "sampler_name": "uni_pc",
                                                    "scheduler": "simple", "denoise": 1.0}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "CreateVideo", "inputs": {"images": ["9", 0], "fps": float(fps)}},
        "11": {"class_type": "SaveVideo", "inputs": {"video": ["10", 0], "filename_prefix": prefix, "format": "mp4", "codec": "h264"}},
    }
    if start_image:
        g["12"] = {"class_type": "LoadImage", "inputs": {"image": start_image}}
        g["7"]["inputs"]["start_image"] = ["12", 0]
    return g


class Comfy:
    def __init__(self, folder: Path, host: str = HOST):
        self.folder = Path(folder)
        self.host = host
        self.process: subprocess.Popen | None = None
        self.client = uuid.uuid4().hex

    def up(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.host}/system_stats", timeout=5) as r:
                return r.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def start(self, timeout: float = 240) -> None:
        if self.up():
            return
        py = self.folder / "venv" / "Scripts" / "python.exe"
        log = (self.folder / "comfyui.log").open("ab")
        self.process = subprocess.Popen([str(py), "main.py", "--listen", "127.0.0.1", "--port", "8188", "--disable-auto-launch",
                                         "--output-directory", str(self.folder / "output")],
                                        cwd=self.folder / "ComfyUI", stdout=log, stderr=log)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.up():
                return
            time.sleep(3)
        raise RuntimeError("ComfyUI did not start")

    def stop(self) -> None:
        if self.process:
            self.process.terminate()
            self.process = None

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(f"{self.host}{path}", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"ComfyUI refused the graph: {e.read()[:800].decode('utf-8', 'replace')}") from e

    def free(self) -> None:
        """Unload the models: stills and video together do not fit in 24 GB."""
        self._post("/free", {"unload_models": True, "free_memory": True})

    def run(self, graph: dict, dest: Path, timeout: float = 1800) -> Path:
        pid = self._post("/prompt", {"prompt": graph, "client_id": self.client})["prompt_id"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            with urllib.request.urlopen(f"{self.host}/history/{pid}", timeout=30) as r:
                hist = json.loads(r.read() or b"{}").get(pid)
            if hist:
                status = hist.get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI failed: {json.dumps(status.get('messages', ''))[:800]}")
                for node in hist.get("outputs", {}).values():
                    for key in ("images", "videos", "gifs", "animated"):
                        for f in node.get(key) or []:
                            if isinstance(f, dict) and f.get("filename"):
                                return self._fetch(f, dest)
                raise RuntimeError(f"ComfyUI finished with no output: {json.dumps(hist.get('outputs', {}))[:500]}")
            time.sleep(2)
        raise RuntimeError(f"ComfyUI timed out after {timeout} s")

    def _fetch(self, f: dict, dest: Path) -> Path:
        q = urllib.parse.urlencode({"filename": f["filename"], "subfolder": f.get("subfolder", ""), "type": f.get("type", "output")})
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(f"{self.host}/view?{q}", timeout=300) as r, dest.open("wb") as fh:
            fh.write(r.read())
        return dest
