# What T3 Code collects: a verified audit

*November 25, 2026. T3 Code (Beta) 0.1.11 for macOS (commit `59651c1`), the official v0.1.11 server
build, and the upstream repository at v0.1.11 and `main`.*

A widely shared thread on X ([@realchendahuang, Nov 24](https://x.com/realchendahuang/status/2125153472450527440))
accused T3 Code of "blatant data harvesting". This audit checks each technical claim against the code
that actually ships and against a real install that had been in daily use for a month. Where the
thread is right, it says so; where it overreaches, it says that too.

## Method

1. **Read the shipped code.** The desktop app's `app.asar` and the standalone server binary, not just
   the repository.
2. **Capture what the official build uploads.** [lab/probe.py](lab/probe.py) runs the official server
   in a sandbox whose `HOME` holds *fake* Codex and Claude logins, with `T3CODE_POSTHOG_HOST` pointed at
   a local sink. Nothing leaves the machine, and the hashes can be checked against known inputs.
3. **Check a real machine.** A Mac where T3 Code had been used daily for a month: its own trace logs,
   settings, and open network connections. Only aggregates are reported here.
4. **Check the history.** Pull requests, issues and commits through the GitHub API.

## The claims, checked

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 1 | PostHog telemetry was added on by default (PR #174, Mar 6) | **True** | [#174](https://github.com/pingdotgg/t3code/pull/174) merged 2026-03-06. In 0.1.11, `T3CODE_TELEMETRY_ENABLED` defaults to `true`. |
| 2 | There is no toggle in the settings; the opt-out is an environment variable | **True** | No setting in 0.1.11. Four community PRs adding one were closed unmerged: [#13458](https://github.com/pingdotgg/t3code/pull/13458), [#16763](https://github.com/pingdotgg/t3code/pull/16763), [#21248](https://github.com/pingdotgg/t3code/pull/21248), [#27077](https://github.com/pingdotgg/t3code/pull/27077). [#1397](https://github.com/pingdotgg/t3code/issues/1397) was closed as not planned: "We will not make this easier to opt out of, it is crucial for us to know how many users we have." |
| 3 | Telemetry is impossible to turn off (a common reading of the thread) | **False** | `T3CODE_TELEMETRY_ENABLED=false` stopped every upload from the official build (0 requests; the telemetry code still ran and returned early). It is documented in [docs/user/telemetry.md](https://github.com/pingdotgg/t3code/blob/main/docs/user/telemetry.md), and an admin policy can also turn it off. |
| 4 | The homepage promised "No telemetry. Unless you opt in. Full stop." until June 18 | **True** | Commit [3bdaa6e1](https://github.com/pingdotgg/t3code/commit/3bdaa6e10046945ccc08264d2d6f3b81775efcb3) (2026-06-18) removed those two lines. |
| 5 | Events are batched every 10 s to a self-hosted `ingest.t3.codes`, which keeps a copy and forwards to PostHog | **True** | `FLUSH_INTERVAL_MS = 1e4`, default host `https://ingest.t3.codes` ([#15028](https://github.com/pingdotgg/t3code/pull/15028)). The upstream docs say the endpoint keeps a copy and forwards to PostHog. |
| 6 | It reports model choice, permission mode, run durations, token counts, project and thread counts, 30+ UI features, OS and device model | **True** | All present in the event code; 31 distinct `feature.used` names. The device model is sent for mobile clients. |
| 6a | ...including the names of private models you connect yourself | **True from 0.1.12** | 0.1.11 still drops custom model names. [#41237](https://github.com/pingdotgg/t3code/pull/41237) (merged Nov 23, in the 0.1.12 nightlies) reports every model ID as is, with `ollama/acme-internal` and `acme-azure-deployment` as test examples. |
| 7 | It reads `~/.codex/auth.json` and `~/.claude.json` and uploads unsalted SHA-256 hashes of your OpenAI user ID, Claude account UUID, T3 Connect account and device ID | **True** | `Identify.ts`. In the captured payload, `distinct_id` = SHA-256(ChatGPT workspace ID) and the owner IDs = SHA-256(OpenAI user ID) and SHA-256(Claude account UUID), all matching the fake inputs exactly ([#21506](https://github.com/pingdotgg/t3code/pull/21506)). |
| 8 | Calling this "anonymous" is misleading | **Fair** | The data is pseudonymous: unsalted hashes of stable account IDs are deterministic, so anyone with the raw ID can match them. T3 does set `$process_person_profile: false`, so PostHog builds no person profiles. |
| 9 | Relay traces go to Axiom, and the browser and mobile clients cannot turn them off | **True** | Upstream docs say so. The desktop app and server honor the opt-out. On the audited Mac, the desktop UI had an open connection to `us-east-1.aws.edge.axiom.co`. |
| 10 | Deleting a thread says it "permanently clears conversation history", but it stays in `statev2.sqlite` | **True on the audited Mac** | A thread deleted 24 days earlier still had 7 messages, 74 turn items and its title in the local database. This is local retention, not an upload. |
| 11 | T3 Connect has no end-to-end encryption | **Not verified** | No app-layer encryption in the code or docs. T3 Connect runs a Cloudflare Tunnel (`cloudflared`) on the host, so TLS ends at Cloudflare's edge. Tailscale sidesteps the question. |
| 12 | Claude Auto mode can run ask-rule commands without prompting | **Open upstream** | [#15353](https://github.com/pingdotgg/t3code/issues/15353) is still open. Not reproduced here. |
| 13 | Using T3 Code with Antigravity gets Google accounts banned | **Not verifiable here** | |

Three things the thread did not mention:

- **`DO_NOT_TRACK=1` is ignored.** The official build uploaded the same events with it set.
- **`T3CODE_OTEL_SDK_DISABLED=true` alone does not stop product analytics.** It only stops relay
  traces and OpenTelemetry exports. The thread's advice still works, because it sets both variables.
- **"Publish agent activity"**, needed for phone notifications, sends thread and project titles to
  T3's relay. The telemetry switch does not cover it.

## A month of normal use (the audited Mac)

- Telemetry was on: no opt-out in the shell, launchd, `service.env` or a policy.
- T3 Code's own trace logs keep about a day. They recorded 39 upload attempts to
  `ingest.t3.codes/batch/` in 8 hours: 14 delivered and 25 that failed DNS resolution (failed
  batches are retried for up to 30 minutes).
- Identity inputs present: a ChatGPT login (`account_id` and `id_token`) in `~/.codex/auth.json`, a
  Claude OAuth login in `~/.claude.json`, and a linked T3 Connect account. So the uploads carried
  SHA-256(ChatGPT workspace ID) as `distinct_id`, and owner hashes for the OpenAI, Claude and
  T3 Connect accounts.
- The desktop UI had contacted `relay.t3.codes`, `clerk.t3.codes` and `us-east-1.aws.edge.axiom.co`.
- "Publish agent activity" was on.

## A captured event

From the official v0.1.11 build in the sandbox. Every ID comes from the fake logins.

```json
{
  "event": "server.boot.heartbeat",
  "distinct_id": "8555534827fefd6ae9bff01fa7139d99998185df1ad6da62357782d18bda9cb6",
  "properties": {
    "threadCount": 0,
    "projectCount": 0,
    "installId": "ce37dac71f648a627ef93aa6e226566e270b0b46cff8fe35c7779546b252bcc4",
    "$process_person_profile": false,
    "platform": "darwin",
    "arch": "arm64",
    "t3CodeVersion": "0.1.11",
    "telemetryChannel": "stable",
    "clientType": "cli-web-client",
    "serverOs": "macOS",
    "serverArch": "arm64",
    "serverAppVersion": "0.1.11",
    "serverMode": "web"
  }
}
```

`8555534827fe...` is `sha256("acct-FAKE-chatgpt-workspace-0001")`, the fake ChatGPT workspace ID.
Two `install.owner_observed` events followed in the same batch, with `ownerSource` `openai` and
`claude` and `ownerId` equal to the SHA-256 of the fake OpenAI user ID and Claude account UUID.

| Setting on the official build | Uploads |
|---|---|
| defaults | yes, within about 10 s of startup |
| `T3CODE_TELEMETRY_ENABLED=false` (what t-off sets) | none |
| `DO_NOT_TRACK=1` | yes |
| `T3CODE_OTEL_SDK_DISABLED=true` | yes |

## Reproduce it

```sh
toff inspect                      # uses your installed T3 Code
python3 lab/probe.py --t3 /path/to/t3 --scenarios default,optout,do-not-track,otel-sdk-disabled
```

## Fair to say

T3 Code's analytics do not include prompts, code or credentials, the upstream docs now describe
the data plainly, and the opt-out works. The problems are the defaults and the friction: on by
default, no switch in the app, `DO_NOT_TRACK` ignored, an analytics ID built from other tools'
accounts, toggle PRs refused, and a "no telemetry" promise removed without an announcement.
