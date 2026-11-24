# t-off

**Telemetry off for T3 Code, re-verified against every new release.**

[![T3 Code opt-out](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/sonozaki7/t-off/main/compat/badge.json)](COMPATIBILITY.md)
[![watchdog](https://github.com/sonozaki7/t-off/actions/workflows/watchdog.yml/badge.svg)](https://github.com/sonozaki7/t-off/actions/workflows/watchdog.yml)
[![ci](https://github.com/sonozaki7/t-off/actions/workflows/ci.yml/badge.svg)](https://github.com/sonozaki7/t-off/actions/workflows/ci.yml)
[![MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

[T3 Code](https://github.com/pingdotgg/t3code) is a great open-source app for running coding agents.
It also uploads usage analytics by default, builds its analytics ID from your ChatGPT and Claude
account IDs, and has no setting to turn any of that off. t-off turns it off with one command,
using the switch T3 Code already supports. A scheduled GitHub Action then re-checks every upstream
release, so nobody has to maintain anything.

> Unofficial and not affiliated with T3 Tools. t-off does not patch, rebuild or redistribute T3 Code:
> you keep the official signed app and its normal updates.

## Install

macOS and Linux:

```sh
curl -fsSL https://raw.githubusercontent.com/sonozaki7/t-off/main/install.sh | sh
```

Then **quit and reopen T3 Code** and run `toff status`. Prefer to read the code first:
`git clone https://github.com/sonozaki7/t-off && cd t-off && ./toff optout`.

Windows (PowerShell), using the same switch:

```powershell
[Environment]::SetEnvironmentVariable('T3CODE_TELEMETRY_ENABLED', 'false', 'User')
# Only if you run the t3 background service:
Add-Content "$HOME\.t3\service.env" 'T3CODE_TELEMETRY_ENABLED=false'
```

## Commands

| Command | What it does |
|---|---|
| `toff optout` | Turn T3 Code's telemetry off (the installer runs this) |
| `toff status` | Settings, running servers, live connections to the trace endpoint, recent uploads |
| `toff audit` | Every upload attempt to `ingest.t3.codes` in T3 Code's own logs, per hour |
| `toff inspect` | Run your installed T3 Code in a sandbox and print exactly what it would upload |
| `toff mobile` | Check and set up the mobile app over Tailscale instead of T3's relay |
| `toff optin` | Turn telemetry back on, after showing what is sent and the risks |
| `toff tailnet on\|off` | Optional strict mode: lock T3 Connect off with a managed policy |
| `toff uninstall` | Remove t-off and everything it changed |

## How it works

T3 Code reads `T3CODE_TELEMETRY_ENABLED`. Set to `false`, it stops product analytics (PostHog, through
`ingest.t3.codes`) and relay tracing (Axiom) from that machine. This is documented upstream in
[docs/user/telemetry.md](https://github.com/pingdotgg/t3code/blob/main/docs/user/telemetry.md).
t-off sets it everywhere T3 Code looks:

| Where | Why |
|---|---|
| `~/.zshenv`, `~/.bash_profile` or `~/.profile`, fish `conf.d` | the desktop app runs `$SHELL -ilc` at launch to read your environment |
| `~/.t3/service.env` | the `t3` background service reads this file |
| launchd (macOS), `environment.d` (Linux) | apps opened from the Dock, Finder or a desktop session |

Each change is a marked block or a single file. `toff optin` and `toff uninstall` remove exactly
those, and symlinked dotfiles stay symlinks.

## Verified against every upstream release

Every 6 hours, [watchdog.yml](.github/workflows/watchdog.yml):

1. downloads the newest stable and nightly official T3 Code server builds and checks them against
   the release's `SHA256SUMS`;
2. runs each one in a sandbox with fake Codex and Claude logins and its analytics pointed at a local
   endpoint ([lab/probe.py](lab/probe.py)). With default settings it must upload, which proves the
   test works. With `T3CODE_TELEMETRY_ENABLED=false` it must upload nothing. It also checks that the
   server accepts the strict-mode policy and stays silent under it;
3. reads the upstream source at that tag and lists analytics events, endpoints compiled into the
   build, the files the telemetry identity code reads, and whether the desktop app still forwards
   the opt-out from your login shell.

Results go to [COMPATIBILITY.md](COMPATIBILITY.md). If a release breaks the opt-out, the watchdog
opens an issue and the run fails. To get notified, watch this repository (Watch, Custom, Issues).
As long as T3 Code keeps its switch, t-off needs no maintenance.

## What T3 Code collects

Verified on T3 Code 0.1.11 by reading the shipped code and capturing what the official build uploads.
Details and evidence: [FINDINGS.md](FINDINGS.md). Run `toff inspect` to see it on your own machine.

- **An ID built from your accounts.** The analytics `distinct_id` is the SHA-256 of your ChatGPT
  workspace ID (read from `~/.codex/auth.json`), or your Claude user ID (`~/.claude.json`). Separate
  `install.owner_observed` events carry SHA-256 hashes of your OpenAI user ID, Claude account UUID,
  T3 Connect account and each connecting device. The hashes are unsalted, so anyone who knows one of
  those IDs can compute the same value.
- **Your agent usage.** For every turn and request: provider, model, reasoning effort, service tier,
  permission mode, durations and token counts (input, cached, cache-creation, output, reasoning).
  From 0.1.12 every model ID is sent as is, including custom and self-hosted model names
  ([#41237](https://github.com/pingdotgg/t3code/pull/41237)).
- **How you use the app.** Thread and project counts at startup, 31 `feature.used` events, MCP tool
  and delegated-task outcomes, OS, CPU architecture, app version, client type, and the device model
  for mobile clients.
- **Relay connection traces** to Axiom when you use T3 Connect: timings, machine name, error text.

Not uploaded: prompts, responses, file contents or credentials. The captured payloads match the
upstream documentation on this.

## What t-off does not cover

- The **web app in a browser** (including app.t3.codes) and the **mobile app** send their own relay
  traces; upstream has no switch for those. Mobile crash reports have a switch in the app.
- **Publish agent activity** (for phone notifications) sends thread and project titles, model names
  and run status to T3's relay. The telemetry switch does not affect it. See [Mobile app](#mobile-app).
- The agents' own telemetry (Claude Code, Codex, the Cursor SDK) is governed by their vendors, for
  example `DISABLE_TELEMETRY=1` for Claude Code.
- Data already sent stays with T3 Tools and PostHog. Ask privacy@t3.tools to access or delete it.

## Mobile app

The official mobile app keeps working. What matters is the route it uses to reach your machine.
The app connects over "the first route that answers": LAN, Tailscale, another VPN, SSH or T3 Connect.

| Setup | Sessions (chats, code, terminal, secrets) | Push notifications | What reaches T3's servers |
|---|---|---|---|
| T3 Connect, T3's default | T3's relay and a Cloudflare tunnel | yes | all session traffic passes through it |
| **Tailscale + T3 Connect "Notifications only"** (recommended) | Tailscale, end to end between your devices | yes | thread and project titles, model names, run status, your T3 Connect sign-in |
| Tailscale only (`toff tailnet on`) | Tailscale | no | nothing |

T3 Connect can be set to **Notifications only** in Settings, Sharing & access.
That mode removes the managed tunnel: push notifications and Live Activities keep working,
but no device can connect through T3's relay. If Tailscale is off on your phone, the app simply cannot
connect instead of silently falling back to the relay.
`toff mobile` checks your machine (Tailscale, MagicDNS, HTTPS certificates, Tailscale HTTPS route,
T3 Connect mode) and lists the steps:

1. In T3 Code: Settings, Sharing & access, turn on **Tailscale HTTPS**
   (command-line hosts: `t3 serve --tailscale-serve` or `t3 pair --tailscale`).
2. Same page, set T3 Connect to **Notifications only**, or **Off** if you don't need notifications.
3. On the phone: install Tailscale, turn on VPN On Demand, pair the machine with the QR from step 1,
   and make the Tailscale route first.
4. In the mobile app: Settings, About T3 Code, Diagnostics, turn off **Send crash reports**.
5. In a browser, open your machine's Tailscale HTTPS address instead of app.t3.codes, so your own
   machine serves the web app.

We could not confirm end-to-end encryption for T3 Connect sessions. T3 Connect runs a Cloudflare
Tunnel on the host, so TLS ends at Cloudflare's edge. Tailscale avoids the question.

## Opting back in

`toff optin` prints what is uploaded and the risks (linkable unsalted hashes, data you cannot easily
recall), then asks you to type `I understand`. `toff optout` turns it off again.

## Strict mode: `toff tailnet on`

Writes a T3 Code [managed policy](https://github.com/pingdotgg/t3code/blob/main/docs/user/managed-policy.md)
(`/Library/Application Support/T3Code/policy.json` or `/etc/t3code/policy.json`, needs sudo) with
`telemetry: false` and `connect: false`. T3 Code shows the machine as managed by "t-off (local policy)" and
enforces the limits, so telemetry and T3 Connect stay off. The trade-off: no push notifications, guest links
or relay webhooks. T3 Code refuses to start if a policy file is invalid, so the watchdog tests this exact
policy against every release. Undo with `toff tailnet off`.

## Why not a fork?

A fork would need its own builds: unsigned binaries, its own update channel, and patches that break
whenever upstream changes. That is ongoing maintenance, and you would be trusting a stranger's
binaries instead of T3's signed ones. T3 Code already has a working off switch. t-off makes it the
default and checks every release to prove it still works.

## FAQ

**Does `DO_NOT_TRACK=1` work?** No. T3 Code's own code ignores it (verified with the official build).

**Does `T3CODE_OTEL_SDK_DISABLED=true` work?** Only for relay traces and your own OpenTelemetry exports.
Product analytics are still uploaded (verified). t-off does not set it, because it would also switch
off any OpenTelemetry export you configured yourself.

**Can I just block `ingest.t3.codes`?** Yes, as a fallback. The batches then fail and are retried and
dropped. The environment switch stops the data at the source, so t-off uses that.

**Will T3 Code updates undo it?** No. The setting lives outside the app, and the watchdog checks every
new release.

**Does it delete data that was already sent?** No. Email privacy@t3.tools (GDPR Articles 15 and 17).

## License

MIT. T3 Code is MIT-licensed software by T3 Tools; the name is used only to describe compatibility.
