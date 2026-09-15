# Redact a leaked secret from local logs and transcripts.
#
#   powershell -File scripts\scrub_secret.ps1 -SecretFile "$env:TEMP\reportscope-scrub-list.txt"
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
# Expect it to take several minutes and look hung. It reads every file under
# the repo, TEMP and ~/.claude -- around 26,000 of them, one of which is a
# 38 MB transcript. That is the point: a secret you did not think to look for
# is exactly the copy that stays valid.
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
    [string[]]$Path,
    [switch]$Apply,
    [switch]$Force
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

# The value file must not be scrubbed. It is the one place the values are
# legitimately written down, and rewriting it mid-run would leave the
# transcript partly cleaned with nothing left to resume from.
$secretFileFull = ''
if ($SecretFile -and (Test-Path $SecretFile)) {
    $secretFileFull = (Resolve-Path $SecretFile).Path
}

# -Path scrubs named files only and skips the tree walk entirely. Use it when
# the exposure has already been located: a full scan reads about 26,000 files
# and takes minutes, and -- more importantly -- it forces every Claude Code
# session closed, because the scan cannot know which transcripts are live.
# Naming one file lets the rest stay open.
$found = @()
if ($Path) {
    foreach ($candidate in $Path) {
        if (Test-Path $candidate) {
            $found += (Resolve-Path $candidate).Path
        } else {
            Write-Warning "  no such file: $candidate"
        }
    }
    Write-Output "Targeted mode: $($found.Count) file(s), no tree scan."
}
elseif ($true) {
foreach ($root in $roots) {
    Get-ChildItem $root -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Length -lt 200MB -and $_.Extension -notin $skipExt -and
                      $_.Name -ne '.env' -and $_.FullName -ne $secretFileFull } |
        ForEach-Object {
            $text = Get-Content $_.FullName -Raw -ErrorAction SilentlyContinue
            if ($text) {
                foreach ($s in $secrets) {
                    if ($text.Contains($s)) { $found += $_.FullName; break }
                }
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

# Is any target still being written to?
#
# A file-lock test was tried first and is useless here: Claude Code does not
# hold a persistent handle on its transcript, so opening it with FileShare::None
# succeeds even mid-session. Measured, not assumed. A guard that always passes
# is worse than none, because it reads as protection.
#
# Last-write time does work. A live session appends on every turn, so a
# transcript touched seconds ago belongs to a session that is still open --
# and rewriting it can leave the owning process appending at a stale offset.
# This cannot prove a session is closed, so it is a tripwire for the ordinary
# mistake (forgetting one tab), overridable with -Force when the operator knows
# better.
$LIVE_WINDOW_SECONDS = 120
$now = Get-Date
$live = @()
foreach ($f in ($found | Select-Object -Unique)) {
    $age = ($now - (Get-Item $f).LastWriteTime).TotalSeconds
    if ($age -lt $LIVE_WINDOW_SECONDS) {
        $live += "{0}  (written {1:N0}s ago)" -f $f, $age
    }
}
if ($live -and -not $Force) {
    Write-Output ""
    Write-Error "These look like live sessions, written to within the last $LIVE_WINDOW_SECONDS seconds:"
    foreach ($f in $live) { Write-Output "  LIVE  $f" }
    Write-Output ""
    Write-Output "Close the session that owns them and run again. Other sessions can stay open."
    Write-Output "Use -Force only if you are certain nothing is appending to these."
    exit 1
}

# A whole-tree scan cannot know which transcripts are live, so it still asks
# for everything closed. A named target has just been proved unlocked.
if (-not $Path) {
    $open = Get-Process -Name "claude*", "node" -ErrorAction SilentlyContinue
    if ($open) {
        Write-Error "Claude Code is running and this is a full-tree scrub. Either close every session, or name the file with -Path (the exposure is usually one transcript)."
        exit 1
    }
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
