#!/usr/bin/env python3
"""t-off watchdog: verify that the newest upstream T3 Code releases still honor the opt-out.

For each upstream release tag (default: latest stable and latest nightly) that has not been
verified yet, it:
  1. downloads the official headless server build (t3-<version>-<platform>.tar.gz) and checks it
     against the release's SHA256SUMS,
  2. runs lab/probe.py: the default run must upload (so the test is real) and the run with
     T3CODE_TELEMETRY_ENABLED=false must upload nothing,
  3. optionally (--policy-test, CI only: needs passwordless sudo) installs the exact managed
     policy that `toff tailnet on` writes and checks the server accepts it and uploads nothing,
  4. reads the upstream source at that tag to list analytics events, telemetry hosts and identity
     sources, and to confirm the desktop app still forwards the opt-out from the login shell,
then records results under compat/ and regenerates COMPATIBILITY.md. When something needs a
human it writes watchdog-alert.md and sets the GitHub Actions outputs alert/alert_title.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

UPSTREAM = "pingdotgg/t3code"
POLICY = {
    "schemaVersion": 1,
    "organization": "t-off (local policy)",
    "supportUrl": "https://github.com/sonozaki7/t-off",
    "limits": {"telemetry": False, "connect": False},
}
LINUX_POLICY_PATH = "/etc/t3code/policy.json"
KEEP_NIGHTLY_RESULTS = 30
# Host names that look like analytics, tracing or error-reporting endpoints.
ENDPOINT_HINT = re.compile(
    r"ingest|posthog|axiom|sentry|segment|amplitude|mixpanel|statsig|datadog|honeycomb|telemetry|analytics|traces|otlp|rudder|heap|bugsnag",
    re.I,
)


def binary_endpoints(path):
    """Analytics-looking https endpoints compiled into the released server binary."""
    pattern = re.compile(rb"https://([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
    found, tail = set(), b""
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8 << 20)
            if not chunk:
                break
            data = tail + chunk
            found.update(m.decode().lower() for m in pattern.findall(data))
            tail = data[-256:]
    return sorted(h for h in found if ENDPOINT_HINT.search(h))


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, **kw)


def gh_api(path):
    return json.loads(run(["gh", "api", path], capture_output=True).stdout)


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def channel_of(tag):
    if "nightly" in tag:
        return "nightly"
    return "prerelease" if "-" in tag else "stable"


def latest_tags():
    tags = [gh_api(f"repos/{UPSTREAM}/releases/latest")["tag_name"]]
    for release in gh_api(f"repos/{UPSTREAM}/releases?per_page=30"):
        if release.get("prerelease") and not release.get("draft") and "nightly" in release["tag_name"]:
            tags.append(release["tag_name"])
            break
    return tags


# ------------------------------------------------------------------ artifact
def download(tag, platform, dest):
    version = tag[1:] if tag.startswith("v") else tag
    asset = f"t3-{version}-{platform}.tar.gz"
    path = os.path.join(dest, asset)
    if not os.path.exists(path):
        run(["gh", "release", "download", tag, "-R", UPSTREAM, "-p", asset, "-p", "SHA256SUMS", "-D", dest, "--clobber"])
    with open(os.path.join(dest, "SHA256SUMS")) as f:
        expected = next((line.split()[0] for line in f if line.strip().endswith(asset)), None)
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    if expected is None or expected != digest.hexdigest():
        raise RuntimeError(f"SHA256 mismatch for {asset}")
    return asset, digest.hexdigest(), path


def extract(archive, dest):
    with tarfile.open(archive) as tf:
        try:
            tf.extractall(dest, filter="data")
        except TypeError:  # Python < 3.12
            tf.extractall(dest)
    for root, _, files in os.walk(dest):
        if "t3" in files and os.access(os.path.join(root, "t3"), os.X_OK):
            return os.path.join(root, "t3")
    raise RuntimeError("no t3 executable in the release archive")


def probe(repo_root, t3, scenarios, out):
    cmd = [sys.executable, "-I", os.path.join(repo_root, "lab", "probe.py"), "--t3", t3,
           "--scenarios", scenarios, "--json-out", out, "--quiet"]
    code = subprocess.run(cmd).returncode
    with open(out) as f:
        report = json.load(f)
    report["exit_code"] = code
    return report


def policy_probe(repo_root, t3, workdir):
    """Install the exact policy `toff tailnet on` writes and check T3 accepts it and stays silent."""
    tmp = os.path.join(workdir, "policy.json")
    with open(tmp, "w") as f:
        json.dump(POLICY, f)
    run(["sudo", "mkdir", "-p", os.path.dirname(LINUX_POLICY_PATH)])
    run(["sudo", "cp", tmp, LINUX_POLICY_PATH])
    try:
        report = probe(repo_root, t3, "default", os.path.join(workdir, "probe-policy.json"))
    finally:
        run(["sudo", "rm", "-f", LINUX_POLICY_PATH])
    scenario = report["scenarios"]["default"]
    return {
        "server_started": scenario["status"] == "ok",
        "uploads": len(scenario["requests"]),
        "status": scenario["status"],
        "server_log_tail": scenario.get("server_log_tail", ""),
    }


# ------------------------------------------------------------------ source checks
def sparse_clone(tag, dest):
    run(["git", "clone", "--quiet", "--depth", "1", "--branch", tag, "--filter=blob:none", "--sparse",
         f"https://github.com/{UPSTREAM}.git", dest], capture_output=True)
    run(["git", "-C", dest, "sparse-checkout", "set", "--no-cone",
         "/apps/server/src/", "/apps/desktop/src/", "/packages/*/src/", "/docs/user/"], capture_output=True)


def read_sources(root, subdir):
    files = {}
    base = os.path.join(root, subdir)
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in ("node_modules", "dist", "__tests__")]
        for name in filenames:
            if name.endswith((".ts", ".tsx", ".mts", ".cts")) and ".test." not in name:
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8", errors="replace") as f:
                    files[os.path.relpath(path, root)] = f.read()
    return files


def source_checks(src):
    server = read_sources(src, "apps/server/src")
    desktop = read_sources(src, "apps/desktop/src")
    packages = read_sources(src, "packages")
    everything = {**server, **desktop, **packages}
    var = "T3CODE_TELEMETRY_ENABLED"

    telemetry_files = {p: s for p, s in everything.items() if re.search(r"telemetry|relayTracing|publicConfig|analytics", p, re.I)}
    hosts = set()
    for text in telemetry_files.values():
        hosts.update(h.lower() for h in re.findall(r"https://([A-Za-z0-9.-]+\.[A-Za-z]{2,})", text))
    hosts = sorted(h for h in hosts if ENDPOINT_HINT.search(h))

    identity_files = {p: s for p, s in server.items() if "telemetry" in p.lower() and re.search(r"identif", p, re.I)}
    identity = set()
    for text in identity_files.values():
        identity.update(re.findall(r'["\'`](\.[A-Za-z0-9_./-]+|[A-Za-z0-9_./-]+\.(?:json|toml|yaml))["\'`]', text))

    policy_files = [s for s in everything.values() if re.search(r"ManagedPolicyDocument\s*=", s)]
    policy_text = "\n".join(policy_files)
    limits = re.search(r"ManagedPolicyLimits\s*=\s*Schema\.Struct\(\{(.*?)\}\)", policy_text, re.S)
    limits_text = limits.group(1) if limits else policy_text

    dims = "\n".join(s for p, s in server.items() if p.endswith("ProviderDimensions.ts"))
    docs_path = os.path.join(src, "docs", "user", "telemetry.md")
    docs = open(docs_path, encoding="utf-8").read() if os.path.exists(docs_path) else ""

    return {
        "server_reads_optout": sorted(p for p, s in server.items() if var in s and re.search(r"posthog|AnalyticsService", s, re.I)),
        "relay_tracing_reads_optout": sorted(p for p, s in everything.items() if "isRelayTracingEnabled" in s and var in s),
        "desktop_forwards_optout_from_login_shell": sorted(p for p, s in desktop.items() if var in s and re.search(r"LOGIN_SHELL|-ilc|shellEnvironment", s, re.I)),
        "service_env_supported": any("service.env" in s for s in server.values()),
        "telemetry_hosts": hosts,
        "events": sorted(set(re.findall(r"\.record\(\s*[\"'`]([a-z][A-Za-z0-9_.]+)[\"'`]", "\n".join(server.values())))),
        "identity_sources": sorted(identity),
        "honors_do_not_track": any("DO_NOT_TRACK" in s for p, s in {**server, **desktop}.items()),
        "custom_model_names_filtered": ("isCustom" in dims) if dims else None,
        "policy_schema_v1": bool(policy_files) and bool(re.search(r"schemaVersion:\s*Schema\.Literal\(1\)", policy_text)),
        "policy_has_telemetry_and_connect": bool(re.search(r"\btelemetry:", limits_text)) and bool(re.search(r"\bconnect:", limits_text)),
        "docs_document_optout": f"{var}=false" in docs,
    }


# ------------------------------------------------------------------ verdicts and reporting
def assess(result, previous):
    probe_verdict = result["probe"]["verdict"] if result.get("probe") else {}
    src = result.get("source") or {}
    critical, warnings, notes = [], [], []

    if result.get("error"):
        warnings.append(f"could not run the check: {result['error']}")
    elif probe_verdict.get("optout_blocks_upload") is False:
        critical.append("with T3CODE_TELEMETRY_ENABLED=false the server still uploaded telemetry")
    elif probe_verdict.get("optout_blocks_upload") is None or not probe_verdict.get("default_uploads"):
        warnings.append("inconclusive: the default run did not upload or a server did not start; the test may need updating")

    policy = result.get("policy")
    if policy is not None:
        if not policy["server_started"]:
            critical.append("the server refused to start with the policy `toff tailnet on` writes (users of tailnet mode must run `toff tailnet off`)")
        elif policy["uploads"]:
            critical.append("with the tailnet policy (telemetry: false) the server still uploaded telemetry")

    prev_bin = (previous or {}).get("binary_endpoints")
    if prev_bin is not None and result.get("binary_endpoints") is not None:
        new_bin = sorted(set(result["binary_endpoints"]) - set(prev_bin))
        if new_bin:
            warnings.append(f"new analytics endpoint(s) compiled into the release: {', '.join(new_bin)}")

    if src:
        if not src.get("desktop_forwards_optout_from_login_shell"):
            warnings.append("the desktop app no longer appears to read T3CODE_TELEMETRY_ENABLED from the login shell")
        if not src.get("relay_tracing_reads_optout"):
            warnings.append("could not find relay tracing honoring T3CODE_TELEMETRY_ENABLED in the source")
        if not src.get("policy_schema_v1") or not src.get("policy_has_telemetry_and_connect"):
            warnings.append("the managed policy schema changed; check `toff tailnet` before using it")
        prev_src = (previous or {}).get("source") or {}
        new_hosts = sorted(set(src.get("telemetry_hosts", [])) - set(prev_src.get("telemetry_hosts", [])))
        if prev_src and new_hosts:
            warnings.append(f"new host(s) in telemetry code: {', '.join(new_hosts)}")
        new_events = sorted(set(src.get("events", [])) - set(prev_src.get("events", [])))
        if prev_src and new_events:
            notes.append(f"new analytics events: {', '.join(new_events)}")
        gone = sorted(set(prev_src.get("events", [])) - set(src.get("events", [])))
        if prev_src and gone:
            notes.append(f"removed analytics events: {', '.join(gone)}")
        new_ids = sorted(set(src.get("identity_sources", [])) - set(prev_src.get("identity_sources", [])))
        if prev_src and new_ids:
            warnings.append(f"telemetry identity code reads new file(s): {', '.join(new_ids)}")
        if src.get("honors_do_not_track"):
            notes.append("T3 Code's own code now mentions DO_NOT_TRACK")

    if critical:
        status = "broken"
    elif any(w.startswith(("could not run", "inconclusive")) for w in warnings):
        status = "inconclusive"
    else:
        status = "verified"
    return status, critical, warnings, notes


def summary(result):
    v = (result.get("probe") or {}).get("verdict") or {}
    events = (result.get("probe") or {}).get("scenarios", {}).get("default", {}).get("analysis", {}).get("event_names", [])
    return {
        "tag": result["tag"],
        "channel": result["channel"],
        "checked_at": result["checked_at"],
        "status": result["status"],
        "optout_blocks_upload": v.get("optout_blocks_upload"),
        "default_uploads": v.get("default_uploads"),
        "do_not_track_honored": v.get("do_not_track_honored"),
        "policy_accepted": None if result.get("policy") is None else (result["policy"]["server_started"] and not result["policy"]["uploads"]),
        "boot_events": events,
        "critical": result["critical"],
        "warnings": result["warnings"],
        "notes": result["notes"],
    }


def yn(value):
    return {True: "yes", False: "**NO**", None: "n/a"}[value]


def render_markdown(status):
    lines = [
        "# Compatibility",
        "",
        "Generated by the [watchdog](.github/workflows/watchdog.yml). Each row is an official T3 Code",
        "release that was downloaded, checked against its `SHA256SUMS`, and run in a sandbox with fake",
        "Codex/Claude logins and a local analytics sink (see [lab/probe.py](lab/probe.py)).",
        "",
        "| Release | Channel | Checked (UTC) | Result | Opt-out stops uploads | Tailnet policy works | DO_NOT_TRACK honored |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in status["history"]:
        badge = {"verified": "verified", "broken": "**BROKEN**", "inconclusive": "inconclusive"}[s["status"]]
        lines.append(f"| [{s['tag']}](https://github.com/{UPSTREAM}/releases/tag/{s['tag']}) | {s['channel']} | "
                     f"{s['checked_at'][:16].replace('T', ' ')} | {badge} | {yn(s['optout_blocks_upload'])} | "
                     f"{yn(s['policy_accepted'])} | {yn(s['do_not_track_honored'])} |")
    for channel in ("stable", "nightly"):
        latest = status["latest"].get(channel)
        if not latest:
            continue
        detail = status.get("details", {}).get(latest["tag"], {})
        src = detail.get("source") or {}
        lines += ["", f"## Latest {channel}: {latest['tag']}", ""]
        if latest["critical"] or latest["warnings"] or latest["notes"]:
            for item in latest["critical"]:
                lines.append(f"- **Critical:** {item}")
            for item in latest["warnings"]:
                lines.append(f"- Warning: {item}")
            for item in latest["notes"]:
                lines.append(f"- Note: {item}")
            lines.append("")
        if detail.get("binary_endpoints") is not None:
            lines.append(f"- Analytics endpoints compiled into the release: {', '.join(f'`{h}`' for h in detail['binary_endpoints']) or 'none found'}")
        if src:
            lines += [
                f"- Analytics hosts in the telemetry source: {', '.join(f'`{h}`' for h in src.get('telemetry_hosts', [])) or 'none found'}",
                f"- Analytics events in the source ({len(src.get('events', []))}): {', '.join(f'`{e}`' for e in src.get('events', []))}",
                f"- Files the telemetry identity code reads: {', '.join(f'`{i}`' for i in src.get('identity_sources', [])) or 'none found'}",
                f"- Custom model names kept out of analytics: {yn(src.get('custom_model_names_filtered'))}",
                f"- Upstream docs describe `T3CODE_TELEMETRY_ENABLED=false`: {yn(src.get('docs_document_optout'))}",
            ]
    return "\n".join(lines) + "\n"


def badge(status):
    latest = status["latest"].get("stable")
    if not latest:
        return {"schemaVersion": 1, "label": "T3 Code opt-out", "message": "not checked yet", "color": "lightgrey"}
    color = {"verified": "brightgreen", "broken": "red", "inconclusive": "yellow"}[latest["status"]]
    message = {"verified": "verified", "broken": "BROKEN", "inconclusive": "unverified"}[latest["status"]]
    return {"schemaVersion": 1, "label": "T3 Code opt-out", "message": f"{latest['tag']} {message}", "color": color}


def set_output(**values):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a") as f:
        for key, value in values.items():
            f.write(f"{key}={value}\n")


def verify_tag(tag, args, previous, workdir):
    result = {"tag": tag, "channel": channel_of(tag), "checked_at": now_iso(), "platform": args.platform}
    tagdir = os.path.join(workdir, re.sub(r"[^A-Za-z0-9._-]", "_", tag))
    os.makedirs(tagdir, exist_ok=True)
    try:
        log(f"== {tag}: downloading the official {args.platform} server build")
        asset, digest, archive = download(tag, args.platform, args.download_dir or tagdir)
        result["asset"], result["sha256"] = asset, digest
        t3 = extract(archive, os.path.join(tagdir, "x"))
        result["binary_endpoints"] = binary_endpoints(t3)
        log(f"== {tag}: probing (default, opt-out, DO_NOT_TRACK)")
        result["probe"] = probe(args.repo_root, t3, "default,optout,do-not-track", os.path.join(tagdir, "probe.json"))
        if args.policy_test:
            log(f"== {tag}: probing with the tailnet managed policy")
            result["policy"] = policy_probe(args.repo_root, t3, tagdir)
        log(f"== {tag}: reading upstream source")
        src_dir = os.path.join(tagdir, "src")
        sparse_clone(tag, src_dir)
        result["source"] = source_checks(src_dir)
    except Exception as exc:  # recorded and reported, never silently dropped
        result["error"] = f"{type(exc).__name__}: {exc}"
        log(f"!! {tag}: {result['error']}")
    result["status"], result["critical"], result["warnings"], result["notes"] = assess(result, previous)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parser.add_argument("--tags", default="", help="comma-separated upstream tags (default: latest stable and nightly)")
    parser.add_argument("--force", action="store_true", help="re-verify tags that were already verified")
    parser.add_argument("--platform", default="linux-x64")
    parser.add_argument("--policy-test", action="store_true", help="also test the tailnet policy (needs passwordless sudo, Linux)")
    parser.add_argument("--download-dir")
    args = parser.parse_args()

    compat = os.path.join(args.repo_root, "compat")
    results_dir = os.path.join(compat, "results")
    os.makedirs(results_dir, exist_ok=True)
    status_path = os.path.join(compat, "status.json")
    status = {"latest": {}, "history": [], "details": {}}
    if os.path.exists(status_path):
        with open(status_path) as f:
            status = json.load(f)
    status.setdefault("details", {})

    tags = [t.strip() for t in args.tags.split(",") if t.strip()] or latest_tags()
    done = {s["tag"]: s for s in status["history"]}
    todo = []
    for tag in tags:
        prior = done.get(tag)
        if args.force or prior is None:
            todo.append(tag)
        elif prior["status"] == "inconclusive" and prior.get("attempts", 1) < 3:
            todo.append(tag)
    log(f"upstream tags: {', '.join(tags)}; to verify: {', '.join(todo) or 'none'}")

    alerts = []
    workdir = tempfile.mkdtemp(prefix="t-off-watchdog-")
    try:
        for tag in todo:
            channel = channel_of(tag)
            prev_summary = status["latest"].get(channel)
            previous = None
            if prev_summary and prev_summary["tag"] != tag:
                prev_path = os.path.join(results_dir, f"{prev_summary['tag']}.json")
                if os.path.exists(prev_path):
                    with open(prev_path) as f:
                        previous = json.load(f)
            result = verify_tag(tag, args, previous, workdir)
            with open(os.path.join(results_dir, f"{tag}.json"), "w") as f:
                json.dump(result, f, indent=2)
            s = summary(result)
            s["attempts"] = (done.get(tag, {}).get("attempts", 0) + 1) if done.get(tag, {}).get("status") == "inconclusive" else 1
            status["history"] = [h for h in status["history"] if h["tag"] != tag]
            status["history"].insert(0, s)
            status["latest"][channel] = s
            status["details"][tag] = {"source": result.get("source"), "binary_endpoints": result.get("binary_endpoints")}
            log(f"== {tag}: {s['status']}")
            if s["critical"] or (s["warnings"] and (s["status"] != "inconclusive" or s["attempts"] >= 2)):
                alerts.append(s)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    # Keep the history bounded: every stable/prerelease, the newest nightlies.
    nightlies = [h for h in status["history"] if h["channel"] == "nightly"]
    for old in nightlies[KEEP_NIGHTLY_RESULTS:]:
        status["history"].remove(old)
        status["details"].pop(old["tag"], None)
        path = os.path.join(results_dir, f"{old['tag']}.json")
        if os.path.exists(path):
            os.remove(path)
    keep = {h["tag"] for h in status["history"]}
    status["details"] = {k: v for k, v in status["details"].items() if k in keep}
    status["updated_at"] = now_iso()

    with open(status_path, "w") as f:
        json.dump(status, f, indent=2)
    with open(os.path.join(compat, "badge.json"), "w") as f:
        json.dump(badge(status), f)
    with open(os.path.join(args.repo_root, "COMPATIBILITY.md"), "w") as f:
        f.write(render_markdown(status))

    broken = any(status["latest"].get(c, {}).get("status") == "broken" for c in ("stable", "nightly"))
    set_output(tags=",".join(todo), alert="true" if alerts else "false", broken="true" if broken else "false")
    if alerts:
        worst = alerts[0]
        kind = "Opt-out BROKEN" if worst["critical"] else "Review needed"
        title = f"{kind}: T3 Code {worst['tag']}"
        body = [f"The watchdog flagged upstream T3 Code release(s) on {now_iso()}.", ""]
        for s in alerts:
            body.append(f"### {s['tag']} ({s['channel']}): {s['status']}")
            body += [f"- **Critical:** {c}" for c in s["critical"]] + [f"- {w}" for w in s["warnings"]] + [f"- Note: {n}" for n in s["notes"]]
            body.append(f"- Details: `compat/results/{s['tag']}.json`")
            body.append("")
        body.append("Until this is resolved, the network-level fallback is to block `ingest.t3.codes` and "
                    "`us-east-1.aws.edge.axiom.co` (see README).")
        with open(os.path.join(args.repo_root, "watchdog-alert.md"), "w") as f:
            f.write("\n".join(body) + "\n")
        set_output(alert_title=title)
        log(f"ALERT: {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
