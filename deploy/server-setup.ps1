# ── MSP API Manager — Server Setup (Windows Server 2019 Hyper-V) ──
# Run this ONCE on the server as Administrator

# 1. Install Docker Desktop or Docker CE (Linux containers mode)
#    Download: https://docs.docker.com/desktop/install/windows-install/
#    OR use Docker CE via WSL2/Hyper-V

# 2. Install Git
winget install --id Git.Git -e --source winget

# 3. Clone the repo
Set-Location "C:\"
git clone https://github.com/<your-org>/<your-repo>.git MSP-Api-Tools
Set-Location "C:\MSP-Api-Tools"

# 4. Copy your .env file (contains API keys — never committed to git)
# You'll need to manually copy or create this:
#   Copy-Item "\\YOUR-DEV-PC\share\.env" ".env"
# Or create it with:
#   notepad .env

# 5. First-time build & run
docker-compose up -d --build

Write-Host "`n✅ Server setup complete. App running at http://localhost:8000" -ForegroundColor Green
Write-Host "Run deploy/pull-and-restart.ps1 to update after pushing from VS." -ForegroundColor Cyan
