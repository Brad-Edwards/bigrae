# APTL and AI Escape Lab post-provisioning runbook

Use this runbook after a new range reaches `READY` and after every range reset. The current images do not preserve all required live changes. Apply the work in small waves. Complete and validate one wave before you start the next wave.

The live assignments and range IDs are in [range-users.md](./range-users.md). The event IDs, scenario IDs, project details, and shared participant password are in [summary.txt](./summary.txt).

## Rules for both lab types

1. Confirm that `gcloud auth print-access-token` succeeds before you create an IAP tunnel. If authentication fails, stop and request a new login.
2. Confirm that the participant's portal range reports `READY`.
3. Use the live range ID from `range-users.md`. Do not infer it from an old event or a former range generation.
4. Apply changes only to the requested range. Do not reset or destroy a range as part of this procedure.
5. Discover container names from the live host. Compose project prefixes change between ranges. Do not hard-code a container prefix.
6. Make each change idempotent. A second run must not duplicate shell configuration or briefing text.
7. Keep no more than four range-fix sessions active at one time. Complete each wave before you provision or fix the next wave.
8. Remove every temporary IAP firewall rule, SSH metadata key, local private key, tunnel, tunnel log, staging file, and temporary kubeconfig when the wave is complete.
9. Mark a range `clean / ready` in `range-users.md` only after every acceptance check passes.

## APTL lab

The APTL desktop host is the control point. The lab is ready only when the desktop guide, Vertex environment, SOC data, browser credentials, containers, and both Claude roles work.

### 1. Check the base deployment

1. Connect to the range desktop host through an IAP SSH tunnel.
2. Run `docker ps` on the desktop host.
3. Confirm that the lab has 28 running containers.
4. Record the Compose project prefix and the actual TheHive and Cortex container names. Use these names in later commands.
5. If the stack is still starting, wait for its normal start process to finish. Do not run the seed while services are still changing state.

### 2. Install the participant guide and credential page

1. Copy the current APTL participant guide to both desktop formats:

   ```text
   /home/atomik/Desktop/APTL Participant Guide.md
   /home/atomik/Desktop/APTL Participant Guide.html
   ```

2. Create `/home/atomik/Desktop/TechVault-Access.html` and its text copy from the current guide credentials. List the canonical Wazuh, TheHive, Cortex, Shuffle, and MISP URLs.
3. Set all desktop files to owner `atomik` and make them readable by the participant.
4. Compare the installed guide checksum with the source guide checksum.
5. Confirm that the HTML files render from `file://` URLs in Epiphany.

### 3. Install the Vertex model environment

The image can write a metadata HTTP 404 page to `/etc/techvault/model-env.sh`. Replace the file even when it already exists.

Write these exports to `/etc/techvault/model-env.sh`:

```bash
export CLAUDE_CODE_USE_VERTEX=1
export ANTHROPIC_VERTEX_PROJECT_ID=prod-lk3ssb
export CLOUD_ML_REGION=us-east5
export ANTHROPIC_MODEL=claude-sonnet-4-6
export ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-4-6
export ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-haiku-4-5
```

Then:

1. Set the file owner and permissions so the `atomik` user can read it.
2. Source the file in a clean shell and confirm all six values.
3. Confirm that the file contains shell exports. It must not contain HTML or a metadata error response.
4. Confirm that the range host service account is enabled and has working Vertex access.

### 4. Start or refresh the red and blue agents

The `red` and `blue` tmux sessions must source `/etc/techvault/model-env.sh` before they start Claude.

1. If the sessions have not started, install the model environment before the normal seat-start process launches them.
2. If the sessions already run with the wrong environment, restart only the `red` and `blue` tmux sessions through the scenario's seat-start script.
3. Do not wipe or reset the lab.
4. Confirm that the Claude process in each session has the expected Vertex variables.
5. If a participant is already connected, tell them that a tmux restart closes the attached red and blue terminal windows. Relaunch the desktop autostart entries in their X session when necessary.

### 5. Refresh the SOC seed

1. Wait until TheHive, Cortex, and their dependencies report healthy.
2. Run the scenario-provided SOC seed procedure again.
3. Supply the dynamically discovered container names. Do not use a container name copied from another range.
4. Confirm that the seed completes without an error.
5. Confirm that the expected SOC data is present after the seed.

### 6. Set the browser credentials

Use the credentials documented in the installed participant guide.

1. Set the documented password for the existing TheHive service user.
2. Confirm that this operation does not replace the TheHive API key used by integrations.
3. Check for the documented Cortex browser user. Create it when it is absent.
4. Set the documented Cortex browser password.
5. Preserve all existing Cortex API-only identities, API keys, and integrations.
6. Submit a real login request to each browser login endpoint. Both requests must return HTTP 200.

### 7. Repair MISP name resolution

MISP redirects the browser to `misp.techvault.local`. Its container address can change when the stack restarts.

1. Discover the running MISP container by its Compose service label.
2. Resolve its current container IP address with `docker inspect`.
3. Replace the managed `misp.techvault.local` entry in `/etc/hosts`.
4. Install and enable a root-owned systemd oneshot service and timer that repeat this refresh every 60 seconds.
5. Confirm that the hostname resolves and that its HTTPS endpoint returns a redirect.

### 8. Repair the Wazuh rule MCP

1. In the shared MCP request schema, allow an API request body to be either an object or a raw string. Wazuh rule files use raw XML.
2. Rebuild the shared MCP package.
3. Configure `create_detection_rule` to send `PUT` to `/rules/files/zzz_purple_loop.xml` on the Wazuh manager API.
4. Use `wazuh-jwt` authentication through `/security/user/authenticate` and set `overwrite=true`.
5. Stop the affected MCP child process. Let Claude start it again from the corrected files. Do not reset the range or restart the participant tmux session.
6. In one canary range, call the MCP tool with a temporary rule. Read the rule through the Wazuh API, then delete it. Do not leave the canary rule installed.

### 9. Configure the browser startup tabs

Set `/usr/local/bin/techvault-open-browser.sh` to open each URL in a separate Epiphany invocation. Repeated invocations attach each page as a tab in the existing browser process. Open these tabs in this order:

1. `file:///home/atomik/Desktop/TechVault-Access.html`
2. `file:///home/atomik/Desktop/APTL%20Participant%20Guide.html`
3. Wazuh
4. TheHive
5. Cortex
6. Shuffle
7. MISP

Launch the script in the active XFCE display when a participant session already exists. Confirm that the browser has seven tabs.

### 10. APTL acceptance checks

A range passes only when all checks succeed:

- Portal state is `READY`.
- Exactly 28 containers run.
- The participant guide, credential page, and their HTML copies exist on the desktop.
- The installed guide checksum matches the source.
- Both `red` and `blue` tmux sessions run.
- Both Claude processes have the required Vertex environment.
- A real Claude request returns exactly `APTL_RANGE_OK`.
- TheHive accepts the guide credentials with HTTP 200.
- Cortex accepts the guide credentials with HTTP 200.
- The SOC seed completed with the live container names.
- Wazuh, TheHive, Cortex, Shuffle, and MISP return the expected HTTP response.
- `misp.techvault.local` resolves to the current MISP container, and its refresh timer is active.
- The rule MCP accepts raw XML and uses the authenticated Wazuh rules-file endpoint.
- A canary rule can be created, read, and deleted through the corrected path.
- The active participant browser has the credential page, guide, and five application tabs.

## AI Escape Lab

The AI Escape participant connects to the `eval-sandbox` container. The lab is ready only when the shell environment, no-prompt launcher, briefing, containers, challenges, and Claude request work.

### 1. Check the base deployment

1. Connect to the range host through an IAP SSH tunnel.
2. Discover the live `eval-sandbox` container name from `docker ps` or its Compose labels.
3. Confirm that the lab has 11 running containers.
4. Confirm that `/app/BRIEFING.md` exists in `eval-sandbox`.

### 2. Configure the participant shell

Add these lines once to `/etc/bash.bashrc` inside `eval-sandbox`:

```bash
export IS_SANDBOX=1
alias claude='claude --dangerously-skip-permissions'
```

Then start a fresh interactive shell and confirm:

```bash
test "$IS_SANDBOX" = 1
alias claude
```

The alias must include `--dangerously-skip-permissions`.

### 3. Install the launcher

Create `/app/start.sh` inside `eval-sandbox` with this content:

```bash
#!/usr/bin/env bash
set -e
export IS_SANDBOX=1
exec claude --dangerously-skip-permissions "$@"
```

Make the file executable. Confirm that the participant can run it from the default `/app` working directory.

### 4. Make the briefing available to Claude

1. Create `/root/.claude` when it is absent.
2. Copy `/app/BRIEFING.md` to `/root/.claude/CLAUDE.md`.
3. Add one human-facing reminder that the participant can ask Claude to show the briefing again.
4. Add one agent directive that tells Claude to re-display or summarize the briefing when the participant asks.
5. Ensure that the final instruction in the participant briefing tells the participant to run either:

   ```text
   claude --dangerously-skip-permissions
   ```

   or:

   ```text
   ./start.sh
   ```

6. Do not add the reminder or directive more than once.
7. Remove `/opt/lab/hint`, `/usr/local/bin/hint`, and `/opt/lab/hints.txt` when they exist.
8. Remove shell aliases and briefing text that refer to local hints. Hints must remain under participant control in the CTF portal.

### 5. AI Escape acceptance checks

A range passes only when all checks succeed:

- Portal state is `READY`.
- Exactly 11 containers run.
- `eval-sandbox` has `IS_SANDBOX=1` in a fresh interactive shell.
- The `claude` alias uses `--dangerously-skip-permissions`.
- `/app/start.sh` has the expected content and is executable.
- `/root/.claude/CLAUDE.md` contains the briefing and reminder.
- No shell hint command, local hint file, hint alias, or briefing hint reference exists.
- A real Claude request returns exactly `AI_RANGE_OK`.
- The event has these seven challenges in this order:
  1. Mirror, Mirror
  2. Escape the Gym
  3. External Data
  4. Reference Confusion
  5. Workload Identity
  6. Follow the Secrets
  7. Steal the Benchmark
- Portal hint counts by challenge are 4, 2, 4, 3, 3, 3, and 4. Hints unlock in order.

## Completion record

For each range, record the username, live range ID, portal state, container count, model-response result, credential result where applicable, and cleanup result. Update `range-users.md` only after the complete acceptance set passes.

These live changes disappear when a range is reset or reprovisioned. Repeat the applicable procedure for the replacement range ID.

## Current model-access constraint

Both scenarios require Claude through Vertex. The Orthanc private model broker is disabled. The current deployment uses the shared range-host service account. The service account must remain enabled for these live labs to work.

The current design exposes a real bearer token to the range through the scenario metadata path. No VPC Service Controls perimeter is active. This is an accepted live-event condition recorded in `summary.txt`; it is not fixed by this runbook.
