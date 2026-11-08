"""Set up the PC side of the social engine in one folder, from official sources only (models.json).

    python setup_pc.py --folder C:/dev/social-pc [--skip-models] [--only comfyui|tools|models|pip]

Idempotent: every step checks what is already there, and downloads resume. Nothing here logs in to anything.
Layout:
    <folder>/ComfyUI/      the ComfyUI repo at the pinned tag
    <folder>/venv/         Python for ComfyUI, the TTS and the batch runner
    <folder>/models/       weights, by ComfyUI folder (extra_model_paths.yaml points ComfyUI here)
    <folder>/tools/        ffmpeg, c2patool
    <folder>/LICENCES.md   what is installed, from where, under which licence
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = json.loads((HERE / "models.json").read_text(encoding="utf-8"))


def run(*cmd: str, cwd: Path | None = None) -> None:
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True)


def fetch(url: str, dest: Path) -> None:
    """curl with resume; a finished file is left alone."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    done = dest.with_suffix(dest.suffix + ".done")
    if dest.is_file() and done.is_file():
        return
    run("curl", "-L", "--fail", "--retry", "5", "--retry-delay", "10", "-C", "-", "-o", str(dest), url)
    done.write_text(url, encoding="utf-8")


def venv_python(folder: Path) -> Path:
    return folder / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def setup_comfyui(folder: Path) -> None:
    c = MANIFEST["comfyui"]
    repo = folder / "ComfyUI"
    if not (repo / ".git").is_dir():
        run("git", "clone", "--depth", "1", "--branch", c["tag"], c["repo"], str(repo))
    py = venv_python(folder)
    if not py.is_file():
        run(sys.executable, "-m", "venv", str(folder / "venv"))
    run(str(py), "-m", "pip", "install", "--upgrade", "pip")
    run(str(py), "-m", "pip", "install", "torch", "torchvision", "torchaudio", "--index-url", MANIFEST["torch_index"])
    run(str(py), "-m", "pip", "install", "-r", str(repo / "requirements.txt"))
    (repo / "extra_model_paths.yaml").write_text(
        "social:\n"
        f"    base_path: {(folder / 'models').as_posix()}\n"
        "    diffusion_models: diffusion_models\n"
        "    text_encoders: text_encoders\n"
        "    vae: vae\n"
        "    loras: loras\n",
        encoding="utf-8",
    )


def setup_pip(folder: Path) -> None:
    run(str(venv_python(folder)), "-m", "pip", "install", *MANIFEST["pip"])


def setup_tools(folder: Path) -> None:
    for t in MANIFEST["tools"]:
        target = folder / "tools" / t["id"]
        if any(target.rglob(t["find"])) if target.is_dir() else False:
            continue
        archive = folder / "downloads" / Path(t["url"]).name
        fetch(t["url"], archive)
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                out = (target / name).resolve()
                if not str(out).startswith(str(target.resolve())):
                    raise SystemExit(f"{archive.name}: unsafe path {name}")
            z.extractall(target)
        print(f"{t['id']}: {next(target.rglob(t['find']))}")


def setup_models(folder: Path) -> None:
    for m in MANIFEST["models"]:
        for f in m["files"]:
            fetch(f["url"], folder / "models" / f["dest"])


def write_licences(folder: Path) -> None:
    c = MANIFEST["comfyui"]
    lines = [
        "# What is installed here, and under which licence",
        "",
        "Written by setup_pc.py from the social engine's `pc/models.json`. Only official sources: the projects' own",
        "repos and the model authors' own Hugging Face pages (Comfy-Org is ComfyUI's own organisation; it repackages",
        "the authors' weights in the single-file format ComfyUI loads). The use is commercial, in the EU.",
        "",
        "| Piece | Job | Source | Licence | Commercial use |",
        "|---|---|---|---|---|",
        f"| ComfyUI {c['tag']} | runs the image and video models, headless | {c['repo']} | {c['licence']} | {c['commercial']} |",
        f"| PyTorch | GPU runtime | {MANIFEST['torch_index']} | BSD-3-Clause | yes |",
    ]
    for t in MANIFEST["tools"]:
        lines.append(f"| {t['id']} | { {'ffmpeg': 'final cut', 'c2patool': 'C2PA marking', 'inter': 'type drawn by code'}.get(t['id'], t['id']) } | {t['source']} | {t['licence']} | {t['commercial']} |")
    for m in MANIFEST["models"]:
        files = "; ".join(f"`{Path(f['dest']).name}` ({f['gb']} GB)" for f in m["files"]) or "downloaded by the package on first use"
        lines.append(f"| {m['id']} | {m['job']} | {m['author']}: {files} | {m['licence']} | {m['commercial']} |")
        for f in m["files"]:
            if f.get("licence"):
                lines.append(f"| ↳ `{Path(f['dest']).name}` | part of {m['id']} | {f['url']} | {f['licence']} | yes |")
    lines += ["", f"**Never:** {MANIFEST['never']}", ""]
    (folder / "LICENCES.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", required=True)
    ap.add_argument("--only", choices=["comfyui", "pip", "tools", "models"])
    ap.add_argument("--skip-models", action="store_true")
    a = ap.parse_args()
    folder = Path(a.folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    if shutil.which("curl") is None:
        raise SystemExit("curl is required")
    steps = {"tools": setup_tools, "models": setup_models, "comfyui": setup_comfyui, "pip": setup_pip}
    for name, step in steps.items():
        if a.only and a.only != name:
            continue
        if name == "models" and a.skip_models:
            continue
        step(folder)
    write_licences(folder)
    print(f"done: {folder}")


if __name__ == "__main__":
    main()
