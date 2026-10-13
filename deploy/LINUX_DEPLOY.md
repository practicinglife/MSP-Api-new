# Linux Deployment Guide — MSP API Tools

Deploy this app to a Linux server with one bootstrap script and a one-shot deploy from your Windows dev machine.

> **Two modes:**
> - **🟢 Dev Mode** — keep editing in Visual Studio on Windows, server runs the containers, changes appear live in ~1 second. Use this if your Windows box can't run Docker + AnythingLLM at the same time. See [Live Dev Mode](#live-dev-mode-recommended-for-your-current-use-case) below.
> - **🔴 Prod Mode** — sealed image, no bind mounts, restart-on-failure. Use after you cut over to production. See [Prod Mode](#prod-mode-when-youre-ready-to-go-live).

---

## Live Dev Mode (recommended for your current use case)

### How it works

```
┌─ Windows (Visual Studio 2026) ──────┐         ┌─ Linux server (Docker host) ──────┐
│                                      │         │                                    │
│  Edit any .py / .html / .js in app/  │  scp on │  /opt/msp-api-tools/app/  ◄────────┤
│        │                              │  save   │       │                            │
│        ▼                              │ ──────► │       ▼ (bind-mounted)             │
│  deploy/sync-to-server.ps1           │         │  uvicorn --reload restarts (~1 s)  │
│  (file watcher + scp)                │         │  https://server-ip:8000/             │
└──────────────────────────────────────┘         └────────────────────────────────────┘
```

Your Windows machine no longer runs Docker — only your editor.
The Linux box does all the container work and Postgres.

### One-time setup

1. **On the Linux server**, do the [server bootstrap](#step-2--one-time-server-bootstrap) below (steps 1–3).

2. **On the Linux server**, start the dev compose stack instead of prod:
   ```bash
   cd /opt/msp-api-tools
   docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
   ```
   This enables:
   - `app/` bind-mount (the synced files become live code)
   - `uvicorn --reload` with file polling (works over SMB/sync)
   - `LOG_LEVEL=DEBUG`
   - Postgres exposed on `5432` so DBeaver/pgAdmin from Windows can connect

3. **On Windows**, set up SSH key auth so the sync script doesn't prompt for a password every save:
   ```powershell
   ssh-keygen -t ed25519 -f $env:USERPROFILE\.ssh\msp-dev -C "msp-dev"
   type $env:USERPROFILE\.ssh\msp-dev.pub | ssh deploy@<server-ip> "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
   ```
   Add to `$env:USERPROFILE\.ssh\config`:
   ```
   Host msp-dev
     HostName <server-ip>
     User deploy
     IdentityFile ~/.ssh/msp-dev
   ```

### Daily workflow

Open a PowerShell tab in your project folder and start the watcher (leave it running):

```powershell
# First time — does a full scp upload then watches for changes
.\deploy\sync-to-server.ps1 -ServerIp msp-dev -InitialFullSync

# Subsequent runs — files already on server, just watch
.\deploy\sync-to-server.ps1 -ServerIp msp-dev
```

Now in Visual Studio:
- Edit any file in `app/`
- Save (`Ctrl+S`)
- See `↑ app/api/companies.py` in the sync console
- Refresh `https://<server-ip>:8000/` — change is live

The watcher ignores `__pycache__`, `.pyc`, `.git`, `~` temp files.

### Why `WATCHFILES_FORCE_POLLING=true`?

When files arrive on the Linux box via `scp` (rather than being edited in place), the kernel doesn't always fire `inotify` events that uvicorn's `--reload` watches by default. Polling mode catches every change but uses ~1% extra CPU — fine for dev.

### Even better: Mutagen (optional, faster)

`scp` per-file is fine for ~5 saves/minute. If you save very frequently or work on many files at once, switch to [Mutagen](https://mutagen.io/documentation/introduction/installation):

```powershell
winget install Mutagen.Mutagen
mutagen sync create --name=msp `
  .\app `
  msp-dev:/opt/msp-api-tools/app `
  --ignore="**/__pycache__,**/*.pyc,**/.git"
mutagen sync monitor msp
```

Mutagen uses persistent SSH connections + delta transfers — sub-100 ms latency per file. Same Docker dev compose works unchanged.

### Connect DBeaver/pgAdmin from Windows

The dev compose exposes Postgres on `5432`:

```
Host:     <server-ip>
Port:     5432
DB:       msp_tools
User:     postgres
Password: postgres   ← change in .env for non-LAN deployments
```

⚠ If the server is internet-facing, do NOT leave Postgres exposed — open only the SSH port and use an SSH tunnel:
```powershell
ssh -L 5432:localhost:5432 deploy@<server-ip>
# Now connect DBeaver to localhost:5432
```

### Switching between dev and prod stacks

```bash
# Stop dev stack
docker compose -f docker-compose.yml -f docker-compose.dev.yml down

# Start prod stack
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

The Postgres `pgdata` volume is shared between modes, so your data persists.

---

## Prod Mode (when you're ready to go live)

Use this once development is settled and you don't need live-edit anymore.

---

## Architecture

```
┌─ Your Windows dev machine (Visual Studio) ─┐
│  git push  ──►  GitHub  ──►  Linux server  │
│  deploy-from-windows.ps1 (single command)  │
└─────────────────────────────────────────────┘
                    │
                    ▼  ssh
┌─ Linux server (Ubuntu 22.04 / 24.04) ──────┐
│  /opt/msp-api-tools/                        │
│    ├── docker-compose.yml                   │
│    ├── docker-compose.prod.yml              │
│    ├── .env  (your secrets, mode 600)       │
│    └── deploy/pull-and-restart.sh           │
│                                             │
│  Containers:                                │
│    ├── app       (Python/FastAPI)  :8000   │
│    └── postgres  (no host port)             │
└─────────────────────────────────────────────┘
```

---

## Step 1 — Spin up the Linux server

Any Ubuntu 22.04 / 24.04 LTS or Debian 12 box with **2 vCPU / 2 GB RAM / 20 GB disk** is plenty.
Confirm SSH is enabled and the user `deploy` exists with sudo rights.

> Production: switch to SSH key auth and disable password login (`PasswordAuthentication no` in `/etc/ssh/sshd_config`).

---

## Step 2 — One-time server bootstrap

SSH in and run the setup script. It installs Docker, clones the repo, configures the firewall, and creates a `.env` template.

```bash
ssh deploy@<server-ip>
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/<your-org>/<your-repo>/master/deploy/server-setup.sh)"
```

…or if you prefer to clone first:

```bash
ssh deploy@<server-ip>
sudo apt-get update && sudo apt-get install -y git
sudo git clone https://github.com/<your-org>/<your-repo>.git /opt/msp-api-tools
cd /opt/msp-api-tools
sudo bash deploy/server-setup.sh
```

The script:
- installs Docker Engine + Compose plugin
- adds `deploy` to the `docker` group (log out/in to take effect)
- clones the repo to `/opt/msp-api-tools`
- creates `.env` from `deploy/.env.production.template` (mode 600, owned by deploy)
- enables `ufw` allowing only SSH and port 8000
- enables unattended security upgrades

---

## Step 3 — Fill in real secrets

```bash
sudo nano /opt/msp-api-tools/.env
```

Populate these blocks (template at `deploy/.env.production.template`):
- `CWM_*` — ConnectWise PSA
- `S1_*` — SentinelOne
- `ESET_*` — ESET Protect Cloud
- `ASIO_*` — Asio / N-able RMM
- `SC_*` — ScreenConnect

Leave `NGROK_AUTHTOKEN` blank unless you actually need a public tunnel — the production compose file disables ngrok by default.

---

## Step 4 — First start

```bash
cd /opt/msp-api-tools
bash deploy/pull-and-restart.sh
```

You should see both containers in `Up` state and `✅ API responding on port 8000` at the end.

Open from another machine: `https://<server-ip>:8000/`

---

## Step 5 — Subsequent deploys (from this Windows machine)

After making changes in Visual Studio, push and deploy in one shot:

```powershell
# From your project folder
.\deploy\deploy-from-windows.ps1 -ServerIp 10.0.0.50

# Skip the git push step (e.g. you already pushed)
.\deploy\deploy-from-windows.ps1 -ServerIp 10.0.0.50 -SkipPush

# Different SSH user / install location
.\deploy\deploy-from-windows.ps1 -ServerIp 10.0.0.50 -SshUser ubuntu -InstallDir /home/ubuntu/msp
```

The script:
1. Stages and commits any local changes (`deploy: 2026-04-19 15:30`)
2. `git push origin master`
3. SSHes to the server and runs `pull-and-restart.sh`
4. Probes `https://server:8000/health` from your machine

---

## SSH key setup (recommended before production)

On your Windows machine:

```powershell
# Generate a key if you don't have one
ssh-keygen -t ed25519 -f $env:USERPROFILE\.ssh\msp-prod -C "msp-deploy"

# Copy the public key to the server (you'll be prompted for the password ONCE)
type $env:USERPROFILE\.ssh\msp-prod.pub | ssh deploy@<server-ip> "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"

# Configure SSH to use this key automatically for the server
@"
Host msp-prod
  HostName <server-ip>
  User deploy
  IdentityFile ~/.ssh/msp-prod
"@ | Add-Content $env:USERPROFILE\.ssh\config

# Now you can shorten everything:
.\deploy\deploy-from-windows.ps1 -ServerIp msp-prod
```

Then on the server, disable password auth:

```bash
sudo sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart ssh
```

---

## Optional: reverse proxy with TLS (Caddy — easiest)

If you want `https://msp.yourdomain.com` instead of `https://ip:8000` (local CA certificate):

```bash
sudo apt-get install -y caddy
echo "msp.yourdomain.com {
  reverse_proxy localhost:8000
}" | sudo tee /etc/caddy/Caddyfile
sudo systemctl reload caddy

# And lock down 8000 to localhost-only:
sudo ufw delete allow 8000/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
```

Caddy auto-issues and renews a Let's Encrypt cert.

---

## Common operations on the server

```bash
# View live logs
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f app

# Restart just the app
docker compose -f docker-compose.yml -f docker-compose.prod.yml restart app

# Open a shell in the running container
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec app bash

# Postgres backup (named volume → file)
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T postgres \
  pg_dump -U postgres msp_tools | gzip > "/opt/backups/msp-$(date +%F).sql.gz"

# Postgres shell
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec postgres psql -U postgres msp_tools

# Stop everything
docker compose -f docker-compose.yml -f docker-compose.prod.yml down

# Reset database (DESTRUCTIVE)
docker compose -f docker-compose.yml -f docker-compose.prod.yml down -v
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `permission denied` running docker | `usermod -aG docker deploy` then log out/in |
| `port 8000 already in use` | `sudo lsof -i :8000` → kill or change `APP_PORT` in `server-setup.sh` |
| Connectors all show `error` | Open `.env` — keys probably blank or wrong base URL |
| WeasyPrint PDF fails | The Dockerfile already installs `libpango`/`libcairo`; if missing, rebuild: `docker compose build --no-cache app` |
| `git pull` fails on server | `cd /opt/msp-api-tools && sudo -u deploy git status` — likely uncommitted local changes |
| `ssh` from Windows asks for password every time | Set up SSH key auth (see above) |
| Need to restore a backup | `gunzip -c msp-2026-04-19.sql.gz \| docker compose exec -T postgres psql -U postgres msp_tools` |

---

## File map

| File | Purpose |
|---|---|
| `deploy/server-setup.sh` | One-time Linux bootstrap (run with sudo) |
| `deploy/pull-and-restart.sh` | Update script, run on the server after each push |
| `deploy/.env.production.template` | Skeleton .env with all required keys |
| `deploy/deploy-from-windows.ps1` | One-shot push + remote update from this VS machine |
| `docker-compose.prod.yml` | Production overrides (no dev mounts, persistent DB) |
| `deploy/README.md` | Original Windows-server notes |
| `deploy/LINUX_DEPLOY.md` | This file |

---

## Security checklist before going live

- [ ] Set a strong password (or SSH keys only) for the deploy user
- [ ] SSH key auth configured, password auth disabled
- [ ] `.env` is mode 600 and owned by the run user
- [ ] Firewall (`ufw status`) only allows SSH + 8000 (or 443 if using a reverse proxy)
- [ ] Postgres port 5432 NOT exposed to the host (the prod compose enforces this)
- [ ] Reverse proxy with TLS in front of port 8000 if it's internet-facing
- [ ] Daily backup of the `pgdata` volume (cron the `pg_dump` one-liner above)
- [ ] Unattended security upgrades enabled (the setup script does this)
- [ ] Real GitHub deploy key (read-only) instead of using your personal credentials
