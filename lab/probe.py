#!/usr/bin/env python3
"""t-off probe: run a real T3 Code server in a throwaway sandbox and record exactly what its
product analytics would upload, without anything leaving the machine.

How it works
  * T3 Code's server reads T3CODE_POSTHOG_HOST, so each run points it at a sink on 127.0.0.1.
  * The sandbox HOME holds *fake* ~/.codex/auth.json and ~/.claude.json logins, so you can see how
    T3 Code turns account identifiers into analytics IDs without exposing real ones.
  * Each scenario starts a fresh server (own T3CODE_HOME, own port), waits until it serves HTTP,
    then waits at least two of T3's 10-second flush windows before stopping it.

Usage
  probe.py --t3 PATH [--scenarios default,optout,do-not-track] [--json-out FILE] [--quiet]
  PATH is a t3 executable (standalone release or the ~/.t3/bin/t3 wrapper) or a macOS
  "T3 Code*.app" bundle, whose server is run with ELECTRON_RUN_AS_NODE=1.

Exit status
  0   opt-out verified: the default run uploaded, the opt-out run uploaded nothing
  10  opt-out FAILED: the opt-out run uploaded data
  11  inconclusive: a server did not start, or the default run uploaded nothing
  2   usage error
"""
import argparse
import base64
import gzip
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FAKE = {
    "codex_account_id": "acct-FAKE-chatgpt-workspace-0001",
    "openai_user_id": "user-FAKE-openai-0001",
    "claude_user_id": "fake-claude-machine-userid-0001",
    "claude_account_uuid": "00000000-0000-4000-8000-fakeclaude001",
}
FAKE_LABELS = {
    "codex_account_id": "ChatGPT workspace account_id (~/.codex/auth.json)",
    "openai_user_id": "OpenAI user_id (id_token in ~/.codex/auth.json)",
    "claude_user_id": "Claude userID (~/.claude.json)",
    "claude_account_uuid": "Claude oauthAccount.accountUuid (~/.claude.json)",
}
SCENARIOS = {
    "default": {},
    "optout": {"T3CODE_TELEMETRY_ENABLED": "false"},
    "do-not-track": {"DO_NOT_TRACK": "1"},
    "otel-sdk-disabled": {"T3CODE_OTEL_SDK_DISABLED": "true"},
}
SETTLE_SECONDS = 30  # T3 flushes every 10 s; 30 s after the port opens covers the boot heartbeat
START_TIMEOUT = 150


def sha256(value):
    return hashlib.sha256(value.encode()).hexdigest()


def b64url(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def write_fake_logins(home):
    os.makedirs(os.path.join(home, ".codex"), exist_ok=True)
    claims = {"https://api.openai.com/auth": {"user_id": FAKE["openai_user_id"]}}
    id_token = ".".join([b64url({"alg": "none", "typ": "JWT"}), b64url(claims), "fake-signature"])
    tokens = {"account_id": FAKE["codex_account_id"], "id_token": id_token, "access_token": "fake", "refresh_token": "fake"}
    with open(os.path.join(home, ".codex", "auth.json"), "w") as f:
        json.dump({"auth_mode": "chatgpt", "tokens": tokens}, f)
    with open(os.path.join(home, ".claude.json"), "w") as f:
        json.dump({"userID": FAKE["claude_user_id"], "oauthAccount": {"accountUuid": FAKE["claude_account_uuid"]}}, f)


class Sink:
    """A PostHog-compatible endpoint on 127.0.0.1 that records every request it receives."""

    def __init__(self):
        self.requests = []
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def _record(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if self.headers.get("Content-Encoding") == "gzip":
                    try:
                        raw = gzip.decompress(raw)
                    except OSError:
                        pass
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = {"_raw": raw[:4000].decode("utf-8", "replace")}
                sink.requests.append({"time": time.time(), "method": self.command, "path": self.path, "body": body})

            def do_POST(self):
                self._record()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":1}')

            do_PUT = do_POST

            def do_GET(self):
                self._record()
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def stop(proc):
    for sig, wait in ((signal.SIGTERM, 8), (signal.SIGKILL, 5)):
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            pass
        try:
            proc.wait(wait)
        except subprocess.TimeoutExpired:
            continue


def t3_command(path):
    """The argv prefix that runs T3 Code's server, plus any extra environment it needs."""
    path = os.path.abspath(os.path.expanduser(path))
    if path.endswith(".app") and os.path.isdir(path):
        macos = os.path.join(path, "Contents", "MacOS")
        exe = next((os.path.join(macos, n) for n in sorted(os.listdir(macos)) if not n.startswith(".")), None)
        server = os.path.join(path, "Contents", "Resources", "app.asar", "apps", "server", "dist", "bin.mjs")
        if exe is None:
            raise SystemExit(f"no executable in {macos}")
        return [exe, server], {"ELECTRON_RUN_AS_NODE": "1"}
    if not os.access(path, os.X_OK):
        raise SystemExit(f"{path} is not executable")
    return [path], {}


def count_spans(t3home):
    counts = {"AnalyticsService.record": 0, "AnalyticsService.sendBatch": 0}
    logs = os.path.join(t3home, "userdata", "logs")
    if not os.path.isdir(logs):
        return counts
    for name in os.listdir(logs):
        if not name.startswith("server.trace.ndjson"):
            continue
        with open(os.path.join(logs, name), encoding="utf-8", errors="replace") as f:
            for line in f:
                for key in counts:
                    if f'"name":"{key}"' in line:
                        counts[key] += 1
    return counts


def run_scenario(name, extra_env, argv, cmd_env, workdir, settle, log):
    root = tempfile.mkdtemp(prefix=f"{name}-", dir=workdir)
    home, t3home = os.path.join(root, "home"), os.path.join(root, "t3home")
    os.makedirs(home)
    os.makedirs(t3home)
    write_fake_logins(home)
    sink = Sink()
    port = free_port()
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": home,
        "T3CODE_HOME": t3home,
        "T3CODE_POSTHOG_HOST": f"http://127.0.0.1:{sink.port}",
        "LANG": "en_US.UTF-8",
    }
    for key in ("USER", "LOGNAME", "TMPDIR"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    env.update(cmd_env)
    env.update(extra_env)
    log_path = os.path.join(root, "server.log")
    log(f"[{name}] starting server (env: {extra_env or 'T3 Code defaults'})")
    with open(log_path, "w") as server_log:
        proc = subprocess.Popen(
            argv + ["serve", "--port", str(port), "--no-browser", home],
            env=env, cwd=home, stdin=subprocess.DEVNULL, stdout=server_log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    started, ready_at, status = time.time(), None, "ok"
    try:
        while time.time() - started < START_TIMEOUT:
            if proc.poll() is not None:
                status = f"server exited with code {proc.returncode} before it was ready"
                break
            if port_open(port):
                ready_at = time.time()
                break
            time.sleep(0.5)
        else:
            status = "server did not start listening in time"
        if ready_at is not None:
            deadline = ready_at + settle
            while time.time() < deadline:
                if proc.poll() is not None:
                    status = f"server exited with code {proc.returncode}"
                    break
                if sink.requests and name != "optout":
                    deadline = min(deadline, sink.requests[0]["time"] + 3)
                time.sleep(0.5)
    finally:
        stop(proc)
        sink.close()
    events = []
    for request in sink.requests:
        body = request["body"] if isinstance(request["body"], dict) else {}
        events.extend(body.get("batch") or [])
    with open(log_path, encoding="utf-8", errors="replace") as f:
        tail = f.read()[-3000:]
    log(f"[{name}] {len(sink.requests)} request(s), {len(events)} event(s)" + ("" if status == "ok" else f"; {status}"))
    return {
        "env": extra_env,
        "status": status,
        "ready_seconds": None if ready_at is None else round(ready_at - started, 1),
        "requests": [{"method": r["method"], "path": r["path"]} for r in sink.requests],
        "events": events,
        "spans": count_spans(t3home),
        "server_log_tail": "" if status == "ok" else tail,
    }


def analyse(events):
    by_hash = {sha256(value): key for key, value in FAKE.items()}
    blob = json.dumps(events)
    owners = {}
    for event in events:
        props = event.get("properties") or {}
        if event.get("event") == "install.owner_observed":
            owners[props.get("ownerSource")] = by_hash.get(props.get("ownerId"), "unrecognised")
    return {
        "event_names": sorted({e.get("event") for e in events if e.get("event")}),
        "property_keys": sorted({k for e in events for k in (e.get("properties") or {})}),
        "distinct_id_is_sha256_of": sorted({by_hash.get(e.get("distinct_id"), "other") for e in events if e.get("distinct_id")}),
        "owner_ids_are_sha256_of": owners,
        "raw_identifier_in_payload": any(value in blob for value in FAKE.values()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--t3", required=True, help="t3 executable or macOS T3 Code .app bundle")
    parser.add_argument("--scenarios", default="default,optout,do-not-track")
    parser.add_argument("--settle", type=int, default=SETTLE_SECONDS)
    parser.add_argument("--json-out")
    parser.add_argument("--keep", action="store_true", help="keep the sandbox directories")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    names = [n.strip() for n in args.scenarios.split(",") if n.strip()]
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        parser.error(f"unknown scenario(s): {', '.join(unknown)}; choose from {', '.join(SCENARIOS)}")
    log = (lambda msg: None) if args.quiet else (lambda msg: print(msg, file=sys.stderr, flush=True))
    argv, cmd_env = t3_command(args.t3)
    workdir = tempfile.mkdtemp(prefix="t-off-probe-")
    try:
        results = {name: run_scenario(name, SCENARIOS[name], argv, cmd_env, workdir, args.settle, log) for name in names}
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)

    for result in results.values():
        result["analysis"] = analyse(result["events"])
        result["events"] = result["events"][:8]  # a sample; every id in it is fake
    uploaded = {name: bool(r["requests"]) for name, r in results.items()}
    healthy = {name: r["status"] == "ok" for name, r in results.items()}
    verdict = {
        "default_uploads": uploaded.get("default"),
        "optout_blocks_upload": (not uploaded["optout"]) if "optout" in uploaded and healthy["optout"] else None,
        "do_not_track_honored": (not uploaded["do-not-track"]) if "do-not-track" in uploaded and healthy["do-not-track"] else None,
        "otel_sdk_disabled_blocks_upload": (not uploaded["otel-sdk-disabled"]) if "otel-sdk-disabled" in uploaded and healthy["otel-sdk-disabled"] else None,
    }
    if "optout" in uploaded and uploaded["optout"]:
        code = 10
    elif not all(healthy.values()) or ("default" in uploaded and not uploaded["default"]):
        code = 11
    else:
        code = 0
    report = {"t3": args.t3, "fake_identities": FAKE, "scenarios": results, "verdict": verdict, "exit_code": code}
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(report, f, indent=2)

    if not args.quiet:
        print_human(report)
    return code


def print_human(report):
    out = sys.stdout
    verdict, scenarios = report["verdict"], report["scenarios"]
    default = scenarios.get("default")
    print("", file=out)
    if default and default["events"]:
        a = default["analysis"]
        print("What your T3 Code uploads with its default settings (captured locally, nothing left this machine):", file=out)
        print(f"  events:      {', '.join(a['event_names'])}", file=out)
        print(f"  properties:  {', '.join(a['property_keys'])}", file=out)
        for key in a["distinct_id_is_sha256_of"]:
            print(f"  distinct_id: SHA-256 of your {FAKE_LABELS.get(key, key)}, no salt", file=out)
        for source, key in a["owner_ids_are_sha256_of"].items():
            print(f"  ownerId ({source}): SHA-256 of your {FAKE_LABELS.get(key, key)}, no salt", file=out)
        if a["raw_identifier_in_payload"]:
            print("  WARNING: a raw account identifier appeared in the payload", file=out)
        print("\nFirst captured event (the ids come from the sandbox's fake logins):", file=out)
        print(json.dumps(default["events"][0], indent=2), file=out)
    print("\nResults", file=out)
    labels = {
        "default_uploads": ("T3 Code defaults upload telemetry", None),
        "optout_blocks_upload": ("T3CODE_TELEMETRY_ENABLED=false (what t-off sets) stops all uploads", None),
        "do_not_track_honored": ("DO_NOT_TRACK=1 stops uploads", None),
        "otel_sdk_disabled_blocks_upload": ("T3CODE_OTEL_SDK_DISABLED=true stops uploads", None),
    }
    for key, (label, _) in labels.items():
        value = verdict.get(key)
        if value is None and key not in ("default_uploads",):
            continue
        mark = {True: "yes", False: "no", None: "could not tell"}[value]
        print(f"  {label}: {mark}", file=out)
    for name, result in scenarios.items():
        if result["status"] != "ok":
            print(f"\n[{name}] {result['status']}\n{result['server_log_tail']}", file=out)


if __name__ == "__main__":
    sys.exit(main())
