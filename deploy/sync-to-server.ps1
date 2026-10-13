# ─────────────────────────────────────────────────────────────────
# MSP API Tools — Live dev sync: Windows ➜ Linux server
#
# Watches your local app/, templates/, static/ folders and pushes any
# changed file to the Linux server via scp. The container's uvicorn
# --reload picks it up within ~1 second.
#
# Edit in Visual Studio as normal. Save. See it on https://server:8000.
#
# Usage:
#   .\deploy\sync-to-server.ps1 -ServerIp 10.0.0.50
#   .\deploy\sync-to-server.ps1 -ServerIp 10.0.0.50 -SshUser deploy
#   .\deploy\sync-to-server.ps1 -ServerIp 10.0.0.50 -InitialFullSync
#
# Stop with Ctrl+C.
# ─────────────────────────────────────────────────────────────────
[CmdletBinding()]
param(
  [Parameter(Mandatory)] [string] $ServerIp,
  [string]   $SshUser    = "deploy",
  [string]   $RemoteRoot = "/opt/msp-api-tools",
  [string[]] $WatchPaths = @("app"),
  [switch]   $InitialFullSync,
  [int]      $DebounceMs = 250
)

$ErrorActionPreference = "Stop"
$LocalRoot = (Get-Location).Path

function Log($msg)  { Write-Host "▶ $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "✔ $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "⚠ $msg" -ForegroundColor Yellow }

# ── 0. Verify SSH ───────────────────────────────────────────────
Log "Testing SSH to ${SshUser}@${ServerIp}"
$probe = ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "${SshUser}@${ServerIp}" "test -d $RemoteRoot && echo ok" 2>&1
if ($LASTEXITCODE -ne 0 -or $probe -notmatch "ok") {
  Write-Error "SSH probe failed or remote path '$RemoteRoot' missing on server: $probe"
  exit 1
}
Ok "SSH ready, remote root exists"

# ── 1. Optional initial full sync ───────────────────────────────
if ($InitialFullSync) {
  Log "Performing initial full sync (this may take a minute)…"
  foreach ($p in $WatchPaths) {
    $local = Join-Path $LocalRoot $p
    if (-not (Test-Path $local)) { continue }
    Log "  scp -r $p → ${ServerIp}:${RemoteRoot}/$p"
    scp -q -r $local "${SshUser}@${ServerIp}:${RemoteRoot}/"
  }
  Ok "Initial sync complete"
}

# ── 2. File watcher loop ────────────────────────────────────────
$watchers = @()
$queue    = New-Object System.Collections.Concurrent.ConcurrentDictionary[string,datetime]

foreach ($p in $WatchPaths) {
  $absPath = Join-Path $LocalRoot $p
  if (-not (Test-Path $absPath)) {
    Warn "Skipping missing path: $p"
    continue
  }

  $fsw = New-Object System.IO.FileSystemWatcher
  $fsw.Path = $absPath
  $fsw.IncludeSubdirectories = $true
  $fsw.EnableRaisingEvents   = $true
  $fsw.NotifyFilter = [System.IO.NotifyFilters]'LastWrite, FileName, DirectoryName'

  $action = {
    $path = $Event.SourceEventArgs.FullPath
    # Skip junk
    if ($path -match '\\__pycache__\\|\.pyc$|\\.git\\|\.tmp$|~$') { return }
    if ((Test-Path $path) -and (Get-Item $path -ErrorAction SilentlyContinue).PSIsContainer) { return }
    [void]$Event.MessageData.AddOrUpdate($path, (Get-Date), { param($k,$v) (Get-Date) })
  }

  Register-ObjectEvent $fsw Changed -Action $action -MessageData $queue | Out-Null
  Register-ObjectEvent $fsw Created -Action $action -MessageData $queue | Out-Null
  Register-ObjectEvent $fsw Renamed -Action $action -MessageData $queue | Out-Null
  $watchers += $fsw
  Ok "Watching $p"
}

if ($watchers.Count -eq 0) { Write-Error "No paths to watch"; exit 1 }

Log "Sync running. Press Ctrl+C to stop."
Write-Host ""

# ── 3. Drain queue with debounce ────────────────────────────────
try {
  while ($true) {
    Start-Sleep -Milliseconds $DebounceMs
    if ($queue.Count -eq 0) { continue }

    # Snapshot then clear
    $batch = @()
    foreach ($k in @($queue.Keys)) {
      $null = [datetime]::MinValue
      if ($queue.TryRemove($k, [ref]$null)) { $batch += $k }
    }
    if ($batch.Count -eq 0) { continue }

    foreach ($file in $batch | Sort-Object -Unique) {
      if (-not (Test-Path $file)) { continue }
      $rel = $file.Substring($LocalRoot.Length).TrimStart('\').Replace('\','/')
      $remoteDir = "$RemoteRoot/" + ($rel -replace '/[^/]+$','')
      # Ensure remote directory exists, then scp
      ssh -o BatchMode=yes -o LogLevel=ERROR "${SshUser}@${ServerIp}" "mkdir -p '$remoteDir'" 2>$null
      scp -q $file "${SshUser}@${ServerIp}:$RemoteRoot/$rel"
      if ($LASTEXITCODE -eq 0) {
        Write-Host ("  ↑ {0}" -f $rel) -ForegroundColor DarkGray
      } else {
        Warn "  scp failed for $rel"
      }
    }
  }
}
finally {
  Log "Cleaning up watchers"
  Get-EventSubscriber | Unregister-Event -ErrorAction SilentlyContinue
  foreach ($w in $watchers) { $w.Dispose() }
}
