"""Port 8765 startup diagnostics: reuse healthy instances, never kill others."""

from __future__ import annotations

import json
import os
import socket
import subprocess

HOST = "127.0.0.1"


def probe_axiom(port: int, timeout: float = 1.5) -> dict | None:
    """Return health JSON if an Axiom bridge already answers on the port."""
    import httpx

    try:
        response = httpx.get(f"http://{HOST}:{port}/health", timeout=timeout)
        data = response.json()
    except Exception:
        return None
    if isinstance(data, dict) and data.get("service") == "axiom-local-bridge":
        return data
    return None


def probe_port(port: int, timeout: float = 0.6) -> bool:
    """True when *something* is listening on the port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((HOST, port)) == 0


def port_owner(port: int) -> str:
    """Read-only lookup of the process bound to the port (Windows). Best effort."""
    lines: list[str] = []
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        netstat = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, text=True, timeout=5, creationflags=creationflags,
        ).stdout
        pids: set[str] = set()
        for line in netstat.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
                pids.add(parts[4])
        for pid in sorted(pids):
            tasklist = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5, creationflags=creationflags,
            ).stdout.strip()
            lines.append(f"PID {pid}: {tasklist.split(',')[0].strip('"')} ")
    except Exception as exc:  # diagnostics must never crash startup
        return f"(owner lookup failed: {exc})"
    return "; ".join(lines) if lines else "(owner could not be identified)"


def conflict_report(port: int) -> str:
    return (
        f"Port {port} on {HOST} is already in use by another program: {port_owner(port)}\n"
        f"Axiom will not terminate unrelated processes.\n"
        f"Options:\n"
        f"  1. If that program IS an older Axiom bridge, open http://{HOST}:{port}/health to confirm.\n"
        f"  2. Stop that program yourself, then start Axiom again.\n"
        f"  3. Run Axiom on another port:  set AXIOM_PORT=8766  (then set the Bridge URL in the extension)."
    )


def run_preflight(port: int, force: bool = False) -> str:
    """Returns 'proceed', 'reuse' or 'blocked'. Prints clear diagnostics."""
    existing = probe_axiom(port)
    if existing and not force:
        print(f"An Axiom bridge is already running and healthy on {HOST}:{port} "
              f"(version {existing.get('version')}, Ollama {'connected' if existing.get('ollama_connected') else 'offline'}).")
        print("Reusing it. To restart with the current code, stop that instance first or run with --force.")
        return "reuse"
    if existing and force:
        print(f"--force: attempting to start anyway; the healthy instance on port {port} will keep serving "
              f"unless it is stopped (Axiom never kills other processes).")
        return "proceed"
    if probe_port(port):
        print("STARTUP BLOCKED: another process is using the port.")
        print(conflict_report(port))
        return "blocked"
    return "proceed"


def parse_args(argv: list[str]) -> bool:
    return "--force" in argv or "-f" in argv
