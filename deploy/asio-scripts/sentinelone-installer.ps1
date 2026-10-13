<#
  Asio script: "sentinelone installer"
  ------------------------------------------------------------------------------------------------
  Installs the SentinelOne agent into the client's SentinelOne site. Used by the MSP app's
  "Redeploy antivirus" change (av_remediate) — the app fills both parameters per client:

    SITETOKEN    the client's SentinelOne site registration token (read from SentinelOne by the app)
    DOWNLOADURL  direct download link to the SentinelOne Windows installer (.exe or .msi) — the same
                 installer for every client; the site token decides which site the machine joins.
                 Set once in the app's .env as MSP_S1_INSTALLER_URL.

  Add in Asio: Automation → Scripts → New script (PowerShell), name exactly "sentinelone installer",
  define two parameters named SITETOKEN and DOWNLOADURL (text). Run as SYSTEM.
  Exit codes: 0 = installed / already installed, 1 = bad parameters, 2 = download failed, 3 = install failed.
#>

$ErrorActionPreference = 'Stop'
$SiteToken   = if ($SITETOKEN)   { $SITETOKEN }   else { $env:SITETOKEN }
$DownloadUrl = if ($DOWNLOADURL) { $DOWNLOADURL } else { $env:DOWNLOADURL }

function Write-Log($msg) { Write-Output ("[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg) }

# Already installed and running? Nothing to do.
$svc = Get-Service -Name 'SentinelAgent' -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq 'Running') {
    Write-Log "SentinelOne agent already installed and running — nothing to do."
    exit 0
}

if ([string]::IsNullOrWhiteSpace($SiteToken) -or [string]::IsNullOrWhiteSpace($DownloadUrl)) {
    Write-Log "Missing parameter: SITETOKEN and DOWNLOADURL are both required."
    exit 1
}

$dir = 'C:\installs\SentinelOne'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$isMsi = $DownloadUrl -match '\.msi(\?|$)'
$file = Join-Path $dir ($(if ($isMsi) { 'SentinelOneInstaller.msi' } else { 'SentinelOneInstaller.exe' }))

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Write-Log "Downloading installer…"
    Invoke-WebRequest -Uri $DownloadUrl -OutFile $file -UseBasicParsing
    if ((Get-Item $file).Length -lt 1MB) { throw "Downloaded file is too small — check DOWNLOADURL is a direct download link." }
} catch {
    Write-Log "Download failed: $($_.Exception.Message)"
    exit 2
}

try {
    if ($isMsi) {
        Write-Log "Installing (MSI)…"
        $p = Start-Process msiexec.exe -ArgumentList "/i `"$file`" SITE_TOKEN=`"$SiteToken`" /qn /norestart" -Wait -PassThru
    } else {
        Write-Log "Installing (EXE)…"
        # Current unified installer syntax; older installers use /SITE_TOKEN= /quiet (tried if the first fails)
        $p = Start-Process $file -ArgumentList "-t `"$SiteToken`" -q" -Wait -PassThru
        if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) {
            Write-Log "Installer returned $($p.ExitCode); retrying with legacy switches…"
            $p = Start-Process $file -ArgumentList "/SITE_TOKEN=$SiteToken /quiet" -Wait -PassThru
        }
    }
    Write-Log "Installer exit code: $($p.ExitCode)"
    Start-Sleep -Seconds 20
    $svc = Get-Service -Name 'SentinelAgent' -ErrorAction SilentlyContinue
    if ($svc) {
        Write-Log "SentinelOne agent service present (status: $($svc.Status)). A reboot may be needed to finish."
        exit 0
    }
    if ($p.ExitCode -in 0, 3010, 1641) { Write-Log "Installer reported success; service not visible yet (reboot pending)."; exit 0 }
    Write-Log "Install did not complete."
    exit 3
} catch {
    Write-Log "Install failed: $($_.Exception.Message)"
    exit 3
} finally {
    Remove-Item $file -Force -ErrorAction SilentlyContinue
}
