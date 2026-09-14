# Redact a leaked secret from local logs and transcripts.
#
#   powershell -File scripts\scrub_secret.ps1 -Secret "the-leaked-value"
#   powershell -File scripts\scrub_secret.ps1 -Secret "..." -Apply
#
# Without -Apply it only reports what it would change.
#
# Run this AFTER closing the Claude Code session that produced the transcript.
# The .jsonl is live session state while a session is open, and rewriting it
# underneath a running process can corrupt it.
#
# Scrubbing local copies is housekeeping, not the fix. The fix is to rotate the
# secret, because any copy you failed to find stays valid until you do.

param(
    [Parameter(Mandatory = $true)][string]$Secret,
    [switch]$Apply
)

$ErrorActionPreference = "SilentlyContinue"

if ($Secret.Length -lt 12) {
    Write-Error "Refusing to scrub a short string: too likely to match unrelated text."
    exit 1
}

$roots = @(
    (Join-Path $PSScriptRoot ".."),
    $env:TEMP,
    (Join-Path $env:USERPROFILE ".claude")
) | Where-Object { Test-Path $_ } | ForEach-Object { (Resolve-Path $_).Path }

# .env is where the secret legitimately lives; rotating replaces it. Binary and
# generated files are skipped rather than rewritten.
$skipExt = @('.parquet', '.pyc', '.pem', '.zip', '.png', '.pdf', '.jpg', '.db')

$found = @()
foreach ($root in $roots) {
    Get-ChildItem $root -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Length -lt 200MB -and $_.Extension -notin $skipExt -and $_.Name -ne '.env' } |
        ForEach-Object {
            $text = Get-Content $_.FullName -Raw -ErrorAction SilentlyContinue
            if ($text -and $text.Contains($Secret)) { $found += $_.FullName }
        }
}

if ($found.Count -eq 0) {
    Write-Output "No occurrences found outside .env."
    exit 0
}

Write-Output "Found in $($found.Count) file(s):"
foreach ($f in $found) { Write-Output "  $f" }

if (-not $Apply) {
    Write-Output ""
    Write-Output "Dry run. Re-run with -Apply to redact."
    exit 0
}

$open = Get-Process -Name "claude*", "node" -ErrorAction SilentlyContinue
if ($open) {
    Write-Warning "Claude Code appears to be running. Close it before rewriting transcripts."
}

foreach ($f in $found) {
    try {
        $text = Get-Content $f -Raw
        ($text -replace [regex]::Escape($Secret), "***REDACTED***") |
            Set-Content $f -NoNewline -Encoding utf8
        Write-Output "  redacted $f"
    } catch {
        Write-Warning "  could not rewrite $f -- $($_.Exception.Message)"
    }
}

Write-Output ""
Write-Output "Done. Rotate the secret as well: a copy you did not find is still valid."
