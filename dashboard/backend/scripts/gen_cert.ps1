#requires -Version 5.1
<#
  gen_cert.ps1 - Generate a self-signed TLS certificate for the Dashboard.

  UI-G1 constraints honored:
    - Supports -DryRun (validates inputs, generates nothing)
    - Supports custom hostname and LAN IP as Subject Alternative Names (SAN)
    - Defaults output to state/dashboard/tls/
    - Restricts private key access to the current user (icacls)
    - Never commits certs/keys (state/ is git-ignored)
    - Does NOT touch the Windows certificate store
    - Does NOT touch the Windows firewall
    - Does NOT actually generate a production cert unless Codex authorizes it
      (i.e. do not run this without -Force once authorized)

  Usage:
    .\gen_cert.ps1 -DryRun -Hostname dashboard.local -LanIP 192.168.1.10
    .\gen_cert.ps1 -Hostname dashboard.local -LanIP 192.168.1.10
#>
param(
    [switch]$DryRun,
    [string]$Hostname = "localhost",
    [string]$LanIP = "",
    [string]$OutDir = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

if (-not $OutDir) {
    $OutDir = Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..\..\..\..")) "state\dashboard\tls"
}

$OutDir = [System.IO.Path]::GetFullPath($OutDir)

Write-Host "[gen-cert] output dir : $OutDir"
Write-Host "[gen-cert] hostname    : $Hostname"
Write-Host "[gen-cert] lan ip      : $($(if ($LanIP) { $LanIP } else { "(none)" }))"

# --- SAN list ---
$sanList = @("DNS:$Hostname")
if ($LanIP) {
    # Accept "1.2.3.4" or "1.2.3.4,5.6.7.8"
    $LanIP -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ } | ForEach-Object {
        $sanList += "IP:$_"
    }
}
$sanCsv = $sanList -join ","

Write-Host "[gen-cert] SAN          : $sanCsv"

# --- Validate CN/SAN syntax on dry-run too ---
if ($Hostname -match "[^a-zA-Z0-9.\-_]") {
    Write-Error "Hostname contains invalid characters: $Hostname"
    exit 1
}
if ($LanIP) {
    $LanIP -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ } | ForEach-Object {
        $parts = $_ -split "\."
        if ($parts.Count -ne 4) {
            Write-Error "Invalid IP: $_"
            exit 1
        }
        foreach ($p in $parts) {
            if (-not ($p -match "^\d+$") -or [int]$p -gt 255) {
                Write-Error "Invalid IP: $_"
                exit 1
            }
        }
    }
}

if ($DryRun) {
    Write-Host "[gen-cert] DRY-RUN: inputs valid, no certificate was generated."
    exit 0
}

if (-not $Force) {
    Write-Host ""
    Write-Host "[gen-cert] This will generate a real self-signed certificate."
    Write-Host "[gen-cert] UI-G1 requires explicit Codex authorization before doing so."
    Write-Host "[gen-cert] Re-run with -Force to actually generate."
    exit 0
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$certPath = Join-Path $OutDir "cert.pem"
$keyPath  = Join-Path $OutDir "key.pem"

# --- Generate self-signed cert (PowerShell's New-SelfSignedCertificate writes to cert store;
#     to avoid touching the store we generate via .NET X509Certificate2 with a PEM export).
#     Alternative approach used below keeps everything in the current user store-less flow. ---
$dn = New-Object System.Security.Cryptography.X509Certificates.X500DistinguishedName("CN=$Hostname")
$rsa = [System.Security.Cryptography.RSA]::Create(2048)
$req = New-Object System.Security.Cryptography.X509Certificates.CertificateRequest($dn, $rsa, [System.Security.Cryptography.HashAlgorithmName]::SHA256, [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)

# Build SAN extension
$sanBuilder = New-Object System.Security.Cryptography.X509Certificates.SubjectAlternativeNameBuilder
$sanBuilder.AddDnsName($Hostname)
if ($LanIP) {
    $LanIP -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ } | ForEach-Object {
        $sanBuilder.AddIpAddress([System.Net.IPAddress]::Parse($_))
    }
}
$sanExt = $sanBuilder.Build($true)
$req.CertificateExtensions.Add($sanExt) | Out-Null
$req.CertificateExtensions.Add((New-Object System.Security.Cryptography.X509Certificates.X509BasicConstraintsExtension($false, $false, 0, $true))) | Out-Null
$req.CertificateExtensions.Add((New-Object System.Security.Cryptography.X509Certificates.X509KeyUsageExtension(
    ([System.Security.Cryptography.X509Certificates.X509KeyUsageFlags]::KeyEncipherment -bor [System.Security.Cryptography.X509Certificates.X509KeyUsageFlags]::DigitalSignature),
    $true))) | Out-Null

$notBefore = [DateTime]::UtcNow.AddDays(-1)
$notAfter  = [DateTime]::UtcNow.AddYears(1)
$cert = $req.CreateSelfSigned($notBefore, $notAfter)

# --- Export PEM files ---
$certPem = [System.Text.Encoding]::UTF8.GetString($cert.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Cert))
# Private key PEM (PKCS#1)
$rsaParams = $rsa.ExportParameters($true)
$pkcs1 = [System.Security.Cryptography.RSA]::Create()
$pkcs1.ImportParameters($rsaParams)
$keyBytes = $pkcs1.ExportRSAPrivateKey()
$b64 = [Convert]::ToBase64String($keyBytes, [System.Base64FormattingOptions]::InsertLineBreaks)
$keyPem = "-----BEGIN RSA PRIVATE KEY-----`n$b64`n-----END RSA PRIVATE KEY-----`n"

[System.IO.File]::WriteAllText($certPath, $certPem, [System.Text.Encoding]::ASCII)
[System.IO.File]::WriteAllText($keyPath, $keyPem, [System.Text.Encoding]::ASCII)

# --- Restrict private key access to current user only ---
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$acl = Get-Acl -Path $keyPath
$acl.SetAccessRuleProtection($true, $false)  # remove inherited rules
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    $sid, "FullControl", "Allow")
$acl.AddAccessRule($rule)
Set-Acl -Path $keyPath -AclObject $acl

# --- Lock down the whole tls dir too ---
$dirAcl = Get-Acl -Path $OutDir
$dirAcl.SetAccessRuleProtection($true, $false)
$dirRule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    $sid, "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow")
$dirAcl.AddAccessRule($dirRule)
Set-Acl -Path $OutDir -AclObject $dirAcl

Write-Host "[gen-cert] certificate : $certPath"
Write-Host "[gen-cert] private key : $keyPath (ACL restricted to current user)"
Write-Host "[gen-cert] NOTE: state/ is git-ignored; certs will never be committed."
Write-Host "[gen-cert] NOTE: Windows cert store and firewall were NOT modified."
