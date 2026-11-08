"""AI marking at export: a C2PA manifest carrying the IPTC DigitalSourceType, signed on the PC with c2patool.

TikTok and Instagram read these to label a post, and the open pipelines do not add them (knowledge/social-native.md).
`init` makes a local signing identity (a small EC P-256 chain) with openssl. Its private key stays in the PC folder,
in no repo. It is not on the C2PA trust list, so a validator shows the signer as unknown; a listed certificate is
a paid product and would need a spend gate. Mark last: any re-encode after it strips the manifest.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

TRAINED = "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"
COMPOSITE = "http://cv.iptc.org/newscodes/digitalsourcetype/compositeWithTrainedAlgorithmicMedia"


def tool(folder: Path, name: str) -> str:
    for hit in (folder / "tools").rglob(f"{name}.exe"):
        return str(hit)
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"{name} is not installed (setup_pc.py)")
    return found


def init_identity(folder: Path, owner: str = "Social engine") -> Path:
    """A root and a leaf certificate for signing. Never overwrites an existing identity."""
    d = folder / "c2pa"
    if (d / "chain.pem").is_file() and (d / "signer.key").is_file():
        return d
    openssl = shutil.which("openssl") or r"C:\Program Files\Git\usr\bin\openssl.exe"
    d.mkdir(parents=True, exist_ok=True)

    def run(*a: str) -> None:
        subprocess.run([openssl, *a], cwd=d, check=True, capture_output=True)

    run("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "root.key")
    run("req", "-x509", "-new", "-key", "root.key", "-sha256", "-days", "3650", "-subj", f"/O={owner}/CN={owner} root",
        "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-out", "root.crt")
    run("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "signer.key")
    run("req", "-new", "-key", "signer.key", "-subj", f"/O={owner}/CN={owner} signer", "-out", "signer.csr")
    (d / "ext.cnf").write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
                               "extendedKeyUsage=emailProtection\nsubjectKeyIdentifier=hash\n"
                               "authorityKeyIdentifier=keyid:always\n", encoding="utf-8")
    run("x509", "-req", "-in", "signer.csr", "-CA", "root.crt", "-CAkey", "root.key", "-CAcreateserial", "-days", "1825",
        "-sha256", "-extfile", "ext.cnf", "-out", "signer.crt")
    (d / "chain.pem").write_text((d / "signer.crt").read_text() + (d / "root.crt").read_text(), encoding="utf-8")
    (d / "root.key").unlink()  # the signer is all we need; nothing else can be issued
    return d


def mark(folder: Path, src: Path, dst: Path, *, composite: bool = False, title: str = "", software: str = "social engine") -> Path:
    identity = init_identity(folder)
    source = COMPOSITE if composite else TRAINED
    manifest = {
        "alg": "es256",
        "private_key": str(identity / "signer.key"),
        "sign_cert": str(identity / "chain.pem"),
        "claim_generator_info": [{"name": software, "version": "1.0"}],
        "title": title or src.name,
        "assertions": [{"label": "c2pa.actions", "data": {"actions": [{
            "action": "c2pa.created", "digitalSourceType": source, "softwareAgent": {"name": software}}]}}],
    }
    mf = dst.with_suffix(".manifest.json")
    mf.write_text(json.dumps(manifest), encoding="utf-8")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run([tool(folder, "c2patool"), str(src), "-m", str(mf), "-o", str(dst), "-f"], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"c2patool: {(r.stderr or r.stdout)[:600]}")
    finally:
        mf.unlink(missing_ok=True)
    return dst


def read(folder: Path, path: Path) -> str:
    r = subprocess.run([tool(folder, "c2patool"), str(path)], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", required=True)
    ap.add_argument("--in", dest="src")
    ap.add_argument("--out", dest="dst")
    ap.add_argument("--init", action="store_true")
    a = ap.parse_args()
    folder = Path(a.folder)
    if a.init:
        print(init_identity(folder))
        return
    print(mark(folder, Path(a.src), Path(a.dst)))


if __name__ == "__main__":
    main()
