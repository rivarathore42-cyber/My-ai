#!/usr/bin/env python3
"""Galaxy AI v6 - one-command Colab launcher.

Colab cell, after uploading galaxy_v6.zip to /content:
    !cd /content && unzip -oq galaxy_v6.zip && python3 galaxy_v6/colab_launcher.py

Steps, in order:
  1. Build the C++ engine from cpp_engine/src/main.cpp with g++ (needed by the C++ bridge).
  2. Install fastapi, uvicorn, ddgs, huggingface_hub and llama-cpp-python (a CUDA wheel first when a GPU exists).
  3. Download the GGUF model from Hugging Face (public repo, no token) if it is not already on disk.
  4. Download cloudflared and open a quick tunnel (no Cloudflare account needed; the URL changes each run).
  5. Start the v6 REST API with a random bearer token, and print the public /api/chat URL and the token.

Every step except the server is optional. When a step fails the launcher says so and continues; the API then
answers with its rule-based fallbacks for whatever is missing.

Environment variables (all optional):
  GALAXY_GGUF_REPO     Hugging Face repo   (default Qwen/Qwen2.5-7B-Instruct-GGUF)
  GALAXY_GGUF_FILE     file inside repo    (default qwen2.5-7b-instruct-q4_k_m.gguf)
  GALAXY_SKIP_LLM=1    skip llama-cpp-python and the model download
  GALAXY_BUILD_RUST=1  also run cargo build --release in rust_engine (cargo must be installed)
  GALAXY_API_TOKEN     use this token instead of a random one
  GALAXY_NO_TUNNEL=1   do not start cloudflared (local URL only)
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODELS = ROOT / "models"
BIN = ROOT / "bin"
LOGS = ROOT / "logs"
PORT = int(os.environ.get("PORT", "8000"))
DEFAULT_REPO = "Qwen/Qwen2.5-7B-Instruct-GGUF"
DEFAULT_FILE = "qwen2.5-7b-instruct-q4_k_m.gguf"
PRESETS = {
    "qwen": ("Qwen/Qwen2.5-7B-Instruct-GGUF", "qwen2.5-7b-instruct-q4_k_m.gguf"),
    # Repo and file names for the R1 distill are not verified against the Hub from this build: set
    # GALAXY_GGUF_REPO / GALAXY_GGUF_FILE if these differ.
    "deepseek-r1": ("bartowski/DeepSeek-R1-Distill-Qwen-7B-GGUF", "DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf"),
}
CUDA_WHEEL_INDEX = "https://abetlen.github.io/llama-cpp-python/whl/cu124"
CLOUDFLARED_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def step(title: str) -> None:
    print(f"\n== {title} ==", flush=True)


def run(cmd, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, text=True, capture_output=True, **kwargs)


def gpu_present() -> bool:
    return shutil.which("nvidia-smi") is not None and run(["nvidia-smi"]).returncode == 0


def build_cpp() -> None:
    step("C++ engine")
    if not shutil.which("g++"):
        print("g++ not found - the C++ bridge stays unavailable")
        return
    out = ROOT / "cpp_engine" / "cpp_engine"
    result = run(["g++", "-std=c++17", "-O3", "-o", str(out), str(ROOT / "cpp_engine" / "src" / "main.cpp")])
    print(f"built {out}" if result.returncode == 0 else "build FAILED:\n" + result.stderr[-800:])


def build_rust() -> None:
    step("Rust engine (optional)")
    if os.environ.get("GALAXY_BUILD_RUST") != "1":
        print("skipped (set GALAXY_BUILD_RUST=1 to build it)")
        return
    cargo = shutil.which("cargo")
    if not cargo:
        print("cargo is not installed - the Rust bridge stays unavailable")
        return
    result = run([cargo, "build", "--release"], cwd=str(ROOT / "rust_engine"))
    print("built" if result.returncode == 0 else "build FAILED:\n" + result.stderr[-800:])


def install_packages() -> None:
    step("Python packages")
    core = run([sys.executable, "-m", "pip", "install", "-q", "fastapi", "uvicorn", "ddgs", "huggingface_hub"])
    print("core packages: ok" if core.returncode == 0 else "core packages FAILED:\n" + core.stderr[-400:])
    if os.environ.get("GALAXY_SKIP_LLM") == "1":
        print("llama-cpp-python: skipped (GALAXY_SKIP_LLM=1)")
        return
    print("llama-cpp-python: if no prebuilt wheel matches this Colab image, pip compiles it from source "
          "(10-20 minutes). Set GALAXY_SKIP_LLM=1 to skip it.")
    if gpu_present():
        gpu = run([sys.executable, "-m", "pip", "install", "-q", "llama-cpp-python", "--prefer-binary",
                   "--extra-index-url", CUDA_WHEEL_INDEX])
        if gpu.returncode == 0:
            print("llama-cpp-python: installed with a CUDA wheel")
            return
        print("no matching CUDA wheel - trying the default build")
    cpu = run([sys.executable, "-m", "pip", "install", "-q", "llama-cpp-python", "--prefer-binary"])
    print("llama-cpp-python: installed" if cpu.returncode == 0
          else "llama-cpp-python FAILED - local LLM disabled, rule-based answers only")


def fetch_model():
    step("Local GGUF model")
    if os.environ.get("GALAXY_SKIP_LLM") == "1":
        print("skipped (GALAXY_SKIP_LLM=1)")
        return None
    preset = os.environ.get("GALAXY_MODEL_PRESET", "qwen")
    preset_repo, preset_file = PRESETS.get(preset, PRESETS["qwen"])
    repo = os.environ.get("GALAXY_GGUF_REPO", preset_repo)
    name = os.environ.get("GALAXY_GGUF_FILE", preset_file)
    print(f"model preset: {preset} -> {repo} / {name}")
    target = MODELS / name
    if target.is_file() and target.stat().st_size > 500_000_000:
        print("already present:", target)
        return target
    try:
        from huggingface_hub import hf_hub_download
        MODELS.mkdir(exist_ok=True)
        path = hf_hub_download(repo_id=repo, filename=name, local_dir=str(MODELS))
        print("downloaded:", path)
        return Path(path)
    except Exception as exc:
        print(f"model download FAILED ({type(exc).__name__}: {str(exc)[:200]}) - rule-based answers only")
        return None


def ensure_cloudflared():
    step("Cloudflare quick tunnel")
    if os.environ.get("GALAXY_NO_TUNNEL") == "1":
        print("skipped (GALAXY_NO_TUNNEL=1)")
        return None
    existing = shutil.which("cloudflared")
    if existing:
        return existing
    BIN.mkdir(exist_ok=True)
    target = BIN / "cloudflared"
    try:
        with urllib.request.urlopen(CLOUDFLARED_URL, timeout=180) as resp, open(target, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        target.chmod(0o755)
        print("downloaded:", target)
        return str(target)
    except Exception as exc:
        print(f"cloudflared download FAILED ({exc}) - local URL only")
        return None


def start_server(token: str, model_path):
    step("Galaxy AI v6 server")
    LOGS.mkdir(exist_ok=True)
    env = {**os.environ, "HOST": "127.0.0.1", "PORT": str(PORT), "GALAXY_API_TOKEN": token,
           "GALAXY_AUTH": os.environ.get("GALAXY_AUTH", "paired")}
    if model_path:
        env["GALAXY_GGUF_PATH"] = str(model_path)
    log = open(LOGS / "server.out", "a", encoding="utf-8")
    return subprocess.Popen([sys.executable, str(ROOT / "run.py")], cwd=str(ROOT), env=env,
                            stdout=log, stderr=subprocess.STDOUT)


def wait_healthy(token: str, server, timeout: float = 120.0) -> dict:
    request_url = f"http://127.0.0.1:{PORT}/api/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if server.poll() is not None:
            raise SystemExit(f"server exited early - see {LOGS / 'server.out'}")
        try:
            req = urllib.request.Request(request_url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(1)
    raise SystemExit(f"server did not become healthy - see {LOGS / 'server.out'}")


def start_tunnel(binary: str):
    proc = subprocess.Popen([binary, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = None
    deadline = time.time() + 90
    for line in proc.stdout:
        match = TUNNEL_RE.search(line)
        if match:
            url = match.group(0)
            break
        if time.time() > deadline:
            break

    def drain() -> None:
        for _ in proc.stdout:
            continue

    threading.Thread(target=drain, daemon=True).start()
    return proc, url


def read_pairing_code():
    """The server prints its pairing code to logs/server.out at boot (and after an admin reissue)."""
    try:
        text = (LOGS / "server.out").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found = [line for line in text.splitlines() if "PAIRING CODE:" in line]
    if not found:
        return None
    digits = "".join(ch for ch in found[-1].split("PAIRING CODE:")[1].split("(")[0] if ch.isdigit())
    return f"{digits[:4]} {digits[4:]}" if len(digits) == 8 else None


def main() -> None:
    token = os.environ.get("GALAXY_API_TOKEN") or secrets.token_urlsafe(24)
    build_cpp()
    build_rust()
    install_packages()
    model_path = fetch_model()
    local_only = os.environ.get("GALAXY_AUTH", "paired") == "local"
    cloudflared = None if local_only else ensure_cloudflared()
    if local_only:
        print("GALAXY_AUTH=local: no public tunnel is opened (a tunnel would expose an open server)")
    server = start_server(token, model_path)
    health = wait_healthy(token, server)

    step("Status")
    print("GPU          :", "present" if gpu_present() else "NOT present (Colab: Runtime > Change runtime type > T4 GPU)")
    print("C++ engine   :", "built" if (ROOT / "cpp_engine" / "cpp_engine").exists() else "missing")
    print("Local LLM    :", health.get("llm", {}).get("state"), "| profile:", health.get("llm", {}).get("profile"),
          "(loads in the background; poll /api/health)")
    print("Database     :", "integrity ok" if health.get("database", {}).get("integrity_ok") else "DEGRADED")

    tunnel = None
    public = None
    if cloudflared:
        step("Public URL")
        tunnel, public = start_tunnel(cloudflared)
        print("quick tunnel:", public or "no URL received in 90 s (check the logs)")
    base = public or f"http://127.0.0.1:{PORT}"
    step("Use it")
    print("API base    :", base)
    print("Open the page and pair this device once with the code below.")
    code = read_pairing_code()
    print("Pairing code:", code or "(see logs/server.out for PAIRING CODE)")
    print("It is valid for 10 minutes and works once. The browser then keeps a private session.")
    print("The operator token is written to logs/server.out for admin use only; chatting never needs it.")
    print("")
    print("\nRunning. Stop the cell to shut everything down.")
    try:
        while server.poll() is None:
            time.sleep(5)
        print("server stopped - see", LOGS / "server.out")
    except KeyboardInterrupt:
        print("stopping...")
    finally:
        server.terminate()
        if tunnel is not None:
            tunnel.terminate()


if __name__ == "__main__":
    main()
