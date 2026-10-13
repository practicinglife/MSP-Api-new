# ─────────────────────────────────────────────────────────────────
# MSP API Tools — one-shot deploy from Windows → Linux server
# Pushes the current branch and runs pull-and-restart.sh on the server.
#
# Usage:
#   .\deploy\deploy-from-windows.ps1 -ServerIp 10.0.0.50
#   .\deploy\deploy-from-windows.ps1 -ServerIp 10.0.0.50 -SshUser deploy
#   .\deploy\deploy-from-windows.ps1 -ServerIp 10.0.0.50 -SkipPush   # already pushed
#
# Prereqs:
#   • OpenSSH client (built into Windows 10/11) — verify with `ssh -V`
#   • Either an SSH key in %USERPROFILE%\.ssh\  OR  you'll be prompted for a password
#   • Server already bootstrapped via deploy/server-setup.sh
# ─────────────────────────────────────────────────────────────────
[CmdletBinding()]
param(
  [Parameter(Mandatory)] [string] $ServerIp,
  [string] $SshUser     = "deploy",
  [string] $InstallDir  = "/opt/msp-api-tools",
  [string] $Branch      = "master",
  [switch] $SkipPush,
  [string] $CommitMessage = "deploy: $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
)

$ErrorActionPreference = "Stop"

function Log($msg) { Write-Host "▶ $msg" -ForegroundColor Cyan }
function Ok($msg)  { Write-Host "✅ $msg" -ForegroundColor Green }
function Warn($msg){ Write-Host "⚠ $msg" -ForegroundColor Yellow }

# ── 1. Push local changes ────────────────────────────────────────
if (-not $SkipPush) {
  Log "Staging & pushing local changes to GitHub"
  $changes = git status --porcelain
  if ($changes) {
    git add -A
    git commit -m $CommitMessage | Out-Null
    Ok "Committed: $CommitMessage"
  } else {
    Warn "No local changes to commit"
  }
  git push origin $Branch
  Ok "Pushed branch '$Branch' to origin"
} else {
  Warn "Skipping git push (--SkipPush)"
}

# ── 2. Verify SSH reachability ──────────────────────────────────
Log "Testing SSH connectivity to ${SshUser}@${ServerIp}"
$test = ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "${SshUser}@${ServerIp}" "echo ok" 2>&1
if ($LASTEXITCODE -ne 0) {
  Write-Error "SSH failed: $test"
  exit 1
}
Ok "SSH reachable"

# ── 3. Run the remote update script ─────────────────────────────
Log "Running pull-and-restart on ${ServerIp}:${InstallDir}"
$remoteCmd = "cd '$InstallDir' && bash deploy/pull-and-restart.sh"
ssh -o StrictHostKeyChecking=accept-new "${SshUser}@${ServerIp}" $remoteCmd
if ($LASTEXITCODE -ne 0) {
  Write-Error "Remote deploy failed (exit $LASTEXITCODE)"
  exit $LASTEXITCODE
}

# ── 4. Health probe from this machine ───────────────────────────
Log "Probing http://${ServerIp}:8000/api/connectors"
try {
  $r = Invoke-WebRequest "http://${ServerIp}:8000/api/connectors" -TimeoutSec 10 -UseBasicParsing
  Ok "API responded HTTP $($r.StatusCode) — deployment successful"
  Write-Host "   Open: http://${ServerIp}:8000/" -ForegroundColor White
} catch {
  Warn "Could not reach API from this machine: $($_.Exception.Message)"
  Warn "(Server may be behind a firewall — check from inside the LAN.)"
}
