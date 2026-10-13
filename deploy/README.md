# MSP API Manager — Deployment Guide

## Architecture

```
┌─────────────────────────┐         ┌──────────────────────────────┐
│  YOUR DEV PC (VS 2026)  │         │  SERVER 2019 (Hyper-V Host)  │
│                         │  git    │                              │
│  Edit code in VS ──────────push──►│  git pull                    │
│  Local Docker for dev   │         │  docker-compose up           │
│  Volume mount (live)    │         │  No volume mount (image)     │
│                         │         │  Persistent Postgres volume  │
└─────────────────────────┘         └──────────────────────────────┘
```

## Workflow

### On your dev PC (Visual Studio)
1. Edit code normally — changes are live via volume mount
2. Test at `http://localhost:8000`
3. When ready, commit and push:
   ```
   git add -A
   git commit -m "fix: PSA mark inactive"
   git push origin master
   ```

### On the server
4. RDP or SSH into the server
5. Run the deploy script:
   ```powershell
   C:\MSP-Api-Tools\deploy\pull-and-restart.ps1
   ```
   This pulls your latest code, rebuilds the Docker image, and restarts.

## Server First-Time Setup

1. **Install Docker Desktop** (with Linux containers via Hyper-V)
2. **Install Git**: `winget install Git.Git`
3. **Clone & configure**:
   ```powershell
   cd C:\
   git clone https://github.com/<your-org>/<your-repo>.git MSP-Api-Tools
   cd MSP-Api-Tools
   # Copy your .env file with API keys
   ```
4. **Start everything**:
   ```powershell
   docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
   ```

## Key Differences: Dev vs Server

| | Dev PC | Server |
|---|---|---|
| Compose files | `docker-compose.yml` | `docker-compose.yml` + `docker-compose.prod.yml` |
| Volume mount | `./app:/app/app` (live edits) | None (uses image code) |
| DB storage | Ephemeral | Persistent `pgdata` volume |
| Restart policy | `unless-stopped` | `always` |
| Code updates | Instant (volume mount) | `git pull` + rebuild |

## Accessing from LAN

The app runs on port 8000. From other machines on your network:
```
http://<server-ip>:8000
```

If you need external access, the ngrok service is already configured in docker-compose.yml.
