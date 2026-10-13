# ── Pull latest code & restart ──
# Run this on the SERVER after pushing from Visual Studio
# Usage: .\deploy\pull-and-restart.ps1

Set-Location "C:\MSP-Api-Tools"

Write-Host "Pulling latest changes..." -ForegroundColor Cyan
git pull origin master

Write-Host "Rebuilding & restarting..." -ForegroundColor Cyan
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build app

Write-Host "`n✅ Deployed. Checking status..." -ForegroundColor Green
Start-Sleep -Seconds 5
docker-compose -f docker-compose.yml -f docker-compose.prod.yml logs --tail=5 app
