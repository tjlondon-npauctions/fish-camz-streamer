# Remote SSH via Cloudflare Zero Trust

We use the same Cloudflare tunnel that serves the Pi's web UI to also expose
SSH. No public IPs, no port forwarding, no per-Pi authorized_keys distribution.
Admins authenticate to Cloudflare Access (Google SSO on the team domain) and
cloudflared issues a short-lived SSH cert for the session.

## Prerequisites (one-time, per Cloudflare account)

1. Sign up for **Cloudflare Zero Trust** (free tier covers up to 50 users).
2. Create a **Team domain** under Settings → Custom Pages — e.g.
   `fishcamz.cloudflareaccess.com`.
3. Add a **login method** (Google Workspace or Google Generic OIDC).

## Per-device setup

Each Pi already runs `cloudflared` as the `rpie-tunnel` service in
`docker-compose.yml`. Its tunnel token is delivered through the heartbeat
response and applied automatically (see `app/heartbeat.py::_apply_tunnel_token`).

In the Cloudflare Zero Trust dashboard:

### 1. Add an SSH public hostname to the tunnel

Networks → Tunnels → click the Pi's tunnel → **Public Hostname** tab → **Add a
public hostname**.

| Field      | Value                              |
|------------|------------------------------------|
| Subdomain  | `<device-name>-ssh` (e.g. `whale-ssh`) |
| Domain     | `fishcamz.com`                     |
| Service    | Type: `SSH`, URL: `localhost:22`   |

Save. cloudflared on the Pi picks up the new ingress within ~30 seconds (no
restart needed — confirmed via the "Updated to new configuration" log line).

### 2. Create an Access application for the hostname

Access → Applications → **Add an application** → **Self-hosted**.

| Field                  | Value                              |
|------------------------|------------------------------------|
| Name                   | `<device-name> SSH`                |
| Session duration       | `24 hours`                         |
| Application domain     | `<device-name>-ssh.fishcamz.com`   |
| Browser rendering      | Enable **Browser SSH**             |

Add a policy:

| Field    | Value                                          |
|----------|------------------------------------------------|
| Action   | Allow                                          |
| Selector | Emails ending in → `@your-team-domain.com`     |

Save. Access caches the policy server-side; no Pi restart needed.

### 3. Record the hostname in the admin UI

In the Fishcamz admin (`/admin/devices/<deviceId>` → Tunnel & SSH section):

- **Tunnel hostname**: `<device-name>-ssh.fishcamz.com`
- **SSH user**: `admin` (or whoever has shell access on that Pi)

Saving here is metadata — it makes the UI show the right copy-paste command
and the browser-SSH link.

## Connecting

### Option A — browser SSH (easiest, no install)

Visit `https://<device-name>-ssh.fishcamz.com/`. Cloudflare Access prompts you
to log in via your team SSO; on success you get a terminal in the browser.

### Option B — cloudflared CLI (faster + persistent)

Install once: `brew install cloudflared` (macOS) or per the official docs.
First-time setup per machine:

```bash
cloudflared access login https://<device-name>-ssh.fishcamz.com
```

Then connect with your normal `ssh` client by adding to `~/.ssh/config`:

```ssh-config
Host whale-ssh
  HostName whale-ssh.fishcamz.com
  User admin
  ProxyCommand cloudflared access ssh --hostname %h
```

Now `ssh whale-ssh` Just Works — and Cloudflare Access enforces auth.

## Revoking access

Access → Users → click a user → Revoke. They lose SSH on every device-bound
application. No `authorized_keys` edit needed on the Pis — Cloudflare's edge
denies the connection.

## Why not key-based SSH directly?

We could distribute authorized_keys via the heartbeat config delivery (same
pattern as the tunnel token), but Zero Trust gives us:

- Centralized audit log (Access shows every SSH session, who, when, source IP)
- Single login revocation across the whole fleet
- Browser-SSH for non-engineers (no key juggling)
- No public ingress at all — the SSH service stays bound to localhost on the Pi
