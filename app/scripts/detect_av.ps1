$ErrorActionPreference = 'SilentlyContinue'

# --- Configuration ---
# The callback URL is injected by the MSP Manager when sending the command.
# Replace {{CALLBACK_URL}} with the actual endpoint before deployment.
$CallbackUrl = '{{CALLBACK_URL}}'
$Hostname    = $env:COMPUTERNAME

# --- Detection Functions ---

function Get-SecurityCenterAV {
    <#
    .SYNOPSIS
        Query Windows Security Center for registered AntiVirus products.
        Works on workstations (root\SecurityCenter2 namespace).
    #>
    $products = @()
    try {
        $avList = Get-CimInstance -Namespace 'root\SecurityCenter2' `
                                  -ClassName 'AntiVirusProduct' `
                                  -ErrorAction Stop
        foreach ($av in $avList) {
            # productState is a bitmask:
            #   Bits 12-15: provider (scanner) state
            #   Bit 12:     enabled/disabled
            #   Bit 4:      definitions up to date
            $state   = $av.productState
            $enabled = (($state -band 0x1000) -ne 0)
            $upToDate = (($state -band 0x10) -eq 0)

            $products += @{
                name      = $av.displayName
                vendor    = ''
                version   = ''
                enabled   = $enabled
                upToDate  = $upToDate
                source    = 'SecurityCenter'
            }
        }
    }
    catch {
        # SecurityCenter2 may not exist on servers
    }
    return $products
}

function Get-InstalledAVFromRegistry {
    <#
    .SYNOPSIS
        Check registry uninstall keys for known AV products.
        Works on both workstations and servers.
    #>
    $products = @()
    $uninstallPaths = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
    )

    # Known AV product patterns
    $avPatterns = @{
        'eset'       = @('ESET Endpoint Security', 'ESET Endpoint Antivirus',
                         'ESET NOD32 Antivirus', 'ESET Smart Security',
                         'ESET Server Security', 'ESET File Security',
                         'ESET Management Agent')
        'sentinelone' = @('Sentinel Agent', 'SentinelOne')
        'defender'    = @('Windows Defender', 'Microsoft Defender')
        'sophos'      = @('Sophos Endpoint Agent', 'Sophos Anti-Virus')
        'webroot'     = @('Webroot SecureAnywhere')
        'bitdefender' = @('Bitdefender Endpoint Security', 'Bitdefender GravityZone')
    }

    foreach ($path in $uninstallPaths) {
        $entries = Get-ItemProperty -Path $path -ErrorAction SilentlyContinue
        foreach ($entry in $entries) {
            $displayName = $entry.DisplayName
            if (-not $displayName) { continue }

            foreach ($vendor in $avPatterns.Keys) {
                foreach ($pattern in $avPatterns[$vendor]) {
                    if ($displayName -like "*$pattern*") {
                        $products += @{
                            name      = $displayName
                            vendor    = $vendor
                            version   = ($entry.DisplayVersion -as [string]) -replace '^$',''
                            enabled   = $null
                            upToDate  = $null
                            source    = 'Registry'
                        }
                        break
                    }
                }
            }
        }
    }
    return $products
}

function Get-AVServiceStatus {
    <#
    .SYNOPSIS
        Check for known AV services and their running state.
    #>
    $services = @()
    $avServices = @{
        'eset'        = @('ekrn', 'ERAAgent', 'esets_service')
        'sentinelone' = @('SentinelAgent', 'SentinelStaticEngine', 'SentinelHelperService')
        'defender'    = @('WinDefend', 'WdNisSvc', 'MsMpSvc')
        'sophos'      = @('SAVService', 'Sophos Agent', 'Sophos Endpoint Defense Service')
        'webroot'     = @('WRSVC')
        'bitdefender' = @('EPSecurityService', 'EPUpdateServer')
    }

    foreach ($vendor in $avServices.Keys) {
        foreach ($svcName in $avServices[$vendor]) {
            $svc = Get-Service -Name $svcName -ErrorAction SilentlyContinue
            if ($svc) {
                $services += @{
                    name      = "$($svc.DisplayName) (service: $svcName)"
                    vendor    = $vendor
                    version   = ''
                    enabled   = ($svc.Status -eq 'Running')
                    upToDate  = $null
                    source    = 'Service'
                }
            }
        }
    }
    return $services
}

function Get-DefenderStatus {
    <#
    .SYNOPSIS
        Get Windows Defender specific status via MpPreference/MpComputerStatus.
    #>
    $result = $null
    try {
        $status = Get-MpComputerStatus -ErrorAction Stop
        $result = @{
            name      = 'Windows Defender'
            vendor    = 'defender'
            version   = ($status.AMProductVersion -as [string])
            enabled   = $status.RealTimeProtectionEnabled
            upToDate  = $status.AntivirusSignatureAge -le 3
            source    = 'DefenderCmdlet'
        }
    }
    catch {
        # Defender cmdlets not available
    }
    return $result
}

# --- Run All Detections ---

$allProducts = @()

# 1. Windows Security Center (workstations)
$scProducts = Get-SecurityCenterAV
if ($scProducts) { $allProducts += $scProducts }

# 2. Registry installed programs
$regProducts = Get-InstalledAVFromRegistry
if ($regProducts) { $allProducts += $regProducts }

# 3. Service status
$svcProducts = Get-AVServiceStatus
if ($svcProducts) { $allProducts += $svcProducts }

# 4. Defender-specific status
$defStatus = Get-DefenderStatus
if ($defStatus) { $allProducts += $defStatus }

# --- Deduplicate by vendor (keep best source) ---
$vendorMap = @{}
foreach ($p in $allProducts) {
    $v = $p.vendor
    if (-not $v) {
        # Try to classify by name
        $n = $p.name.ToLower()
        if     ($n -match 'eset')       { $v = 'eset' }
        elseif ($n -match 'sentinel')   { $v = 'sentinelone' }
        elseif ($n -match 'defender')   { $v = 'defender' }
        elseif ($n -match 'sophos')     { $v = 'sophos' }
        elseif ($n -match 'webroot')    { $v = 'webroot' }
        elseif ($n -match 'bitdefender'){ $v = 'bitdefender' }
        else                            { $v = 'unknown' }
    }

    if (-not $vendorMap.ContainsKey($v)) {
        $vendorMap[$v] = $p
        $vendorMap[$v].vendor = $v
    }
    else {
        # Merge: prefer non-null enabled/upToDate
        $existing = $vendorMap[$v]
        if ($null -eq $existing.enabled -and $null -ne $p.enabled) {
            $existing.enabled = $p.enabled
        }
        if ($null -eq $existing.upToDate -and $null -ne $p.upToDate) {
            $existing.upToDate = $p.upToDate
        }
        if (-not $existing.version -and $p.version) {
            $existing.version = $p.version
        }
    }
}

# --- Build Result ---
$result = @{
    hostname   = $Hostname
    timestamp  = (Get-Date -Format 'o')
    os_version = [System.Environment]::OSVersion.VersionString
    av_products = @($vendorMap.Values | ForEach-Object {
        @{
            vendor    = $_.vendor
            product   = $_.name
            version   = $_.version
            enabled   = $_.enabled
            up_to_date = $_.upToDate
            source    = $_.source
        }
    })
}

$json = $result | ConvertTo-Json -Depth 5 -Compress

# --- Output for capture ---
Write-Output "AV_DETECT_BEGIN"
Write-Output $json
Write-Output "AV_DETECT_END"

# --- Callback to MSP Manager API (if URL configured) ---
if ($CallbackUrl -and $CallbackUrl -ne '{{CALLBACK_URL}}') {
    try {
        $headers = @{ 'Content-Type' = 'application/json' }
        Invoke-RestMethod -Uri $CallbackUrl -Method POST -Body $json -Headers $headers -TimeoutSec 10
    }
    catch {
        # Callback failure is non-fatal — results still in output
    }
}
