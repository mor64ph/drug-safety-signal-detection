# Redact a leaked secret from local logs and transcripts.
#
#   powershell -File scripts\scrub_secret.ps1 -SecretFile "$env:TEMPeportscope-scrub-list.txt"
#   powershell -File scripts\scrub_secret.ps1 -SecretFile "..." -Apply
#   powershell -File scripts\scrub_secret.ps1 -Secret "one-value" -Apply
#
# -SecretFile takes one value per line. Prefer it over -Secret: a value passed
# on the command line lands in PowerShell history, in the console scrollback,
# and -- if a Claude Code session is open -- in the very transcript you are
# trying to clean. Delete the file afterwards.
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
    [string]$Secret,
    [string]$SecretFile,
    [switch]$Apply
)

$secrets = @()
if ($SecretFile) {
    if (-not (Test-Path $SecretFile)) {
        Write-Error "No such file: $SecretFile"
        exit 1
    }
    $secrets += Get-Content $SecretFile | Where-Object { $_.Trim().Length -gt 0 }
}
if ($Secret) { $secrets += $Secret }
if ($secrets.Count -eq 0) {
    Write-Error "Give -SecretFile or -Secret."
    exit 1
}
$secrets = $secrets | ForEach-Object { $_.Trim() } | Select-Object -Unique

$ErrorActionPreference = "SilentlyContinue"

foreach ($s in $secrets) {
    if ($s.Length -lt 12) {
        Write-Error "Refusing to scrub a string shorter than 12 characters: too likely to match unrelated text."
        exit 1
    }
}
Write-Output "Scrubbing $($secrets.Count) value(s). Lengths: $(($secrets | ForEach-Object { $_.Length }) -join ', ')"

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
            if ($text) {
                foreach ($s in $secrets) {
                    if ($text.Contains($s)) { $found += $_.FullName; break }
                }
            }
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

# Refuse, rather than warn. A .jsonl is live session state: rewriting a 38 MB
# transcript underneath a running process can corrupt the session, and anything
# removed while the session continues can simply be written again.
$open = Get-Process -Name "claude*", "node" -ErrorAction SilentlyContinue
if ($open) {
    Write-Error "Claude Code appears to be running. Close every session first -- rewriting a live transcript can corrupt it, and the value would be re-added anyway."
    exit 1
}

foreach ($f in $found | Select-Object -Unique) {
    try {
        $text = Get-Content $f -Raw
        $hits = 0
        foreach ($s in $secrets) {
            $before = $text.Length
            $text = $text -replace [regex]::Escape($s), "***REDACTED***"
            if ($text.Length -ne $before) { $hits++ }
        }
        Set-Content -Path $f -Value $text -NoNewline -Encoding utf8
        Write-Output "  redacted $hits value(s) in $f"
    } catch {
        Write-Warning "  could not rewrite $f -- $($_.Exception.Message)"
    }
}

Write-Output ""
Write-Output "Done. Delete the secret file now:"
if ($SecretFile) { Write-Output "  Remove-Item '$SecretFile'" }
Write-Output "Rotate anything still valid -- a copy you did not find is still a copy."
