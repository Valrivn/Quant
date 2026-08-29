# Supervised pipeline runner: starts scrape_cmd --active, watches for
# degradation, fires milestone/issue notifications, and writes a status file
# for the pipeline-supervisor agent.
param(
    [int]$PollSeconds = 15,
    [int]$MaxWaitMinutes = 60,
    [string]$LogFile = "logs/supervised_pipeline.log",
    [string]$StatusFile = "logs/supervised_pipeline_status.json",
    [string]$ConsensusStatusFile = "logs/consensus_status.json",
    [int]$ConsensusLimit = 1,
    [switch]$Live,
    [switch]$LiveConsensus
)

if ($Live) { $env:DISCOVERY_LIVE = "1" }

$pyArgs = "scripts/scrape_cmd.py --active --consensus-limit $ConsensusLimit"
if ($LiveConsensus) {
    $pyArgs = "scripts/scrape_cmd.py --active --live-consensus --consensus-limit $ConsensusLimit"
    $env:DISCOVERY_LIVE = "1"
}

function Write-ConsensusStatus {
    param($Ticker, $Usable, $Reviews, $Flags, $Score, $State)
    $obj = @{
        ticker = $Ticker
        usable_sources = $Usable
        total_reviews = $Reviews
        flags = $Flags
        score = $Score
        live_consensus = [bool]$LiveConsensus
        state = $State
        watched_at = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ")
    }
    $obj | ConvertTo-Json | Set-Content -Path (Join-Path $root $ConsensusStatusFile)
}

$ErrorActionPreference = "Continue"
$root = "C:\Users\Hayden\Quant"
$notify = Join-Path $root "scripts\notify.ps1"

$logDir = Split-Path -Parent $LogFile
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$outLog = Join-Path $root $LogFile
$errLog = "$outLog.err"

if (Test-Path $outLog) { Remove-Item $outLog -Force }
if (Test-Path $errLog) { Remove-Item $errLog -Force }

function Write-Status {
    param([string]$State, [string]$Detail, [int]$Streak)
    $obj = @{
        state = $State
        detail = $Detail
        issue_streak = $Streak
        watched_at = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ")
    }
    $obj | ConvertTo-Json | Set-Content -Path (Join-Path $root $StatusFile)
}

$proc = Start-Process -FilePath "python" `
    -ArgumentList $pyArgs `
    -WorkingDirectory $root `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog `
    -PassThru -NoNewWindow

$start = Get-Date
Write-Output "SUPERVISOR-START pid=$($proc.Id) at $start args=$pyArgs"
Write-Status -State "running" -Detail "pipeline started pid=$($proc.Id)" -Streak 0

$issueStreak = 0
$milestoneShown = $false
$igWarned = $false
$consensusWarned = $false

while (-not $proc.HasExited) {
    Start-Sleep -Seconds $PollSeconds
    $tail = if (Test-Path $outLog) { Get-Content $outLog -Tail 25 } else { @() }
    $errTail = if (Test-Path $errLog) { Get-Content $errLog -Tail 10 } else { @() }
    $joined = (($tail + $errTail) -join "`n")

    # HARD signals: uncaught tracebacks, fail-hard markers, challenge
    $hard = $joined -match "Traceback|FAIL-HARD|InstagramChallengeDetected|challenge_detected"
    if ($hard) {
        $issueStreak++
        if ($issueStreak -ge 3) {
            Write-Status -State "stopped" -Detail "persistent hard degradation: $($Matches[0])" -Streak $issueStreak
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
            & $notify -Title "Quant Pipeline ISSUE" -Message "Pipeline degraded (challenge/traceback persisted). Watch stopped the run. Check $LogFile" 2>$null
            Write-Output "SUPERVISOR-STOPPED hard_degradation"
            exit 1
        }
    } else {
        $issueStreak = 0
    }

    # SOFT signals: source-level failures (IG needs port 9222; consensus may
    # partially fail) - notify once, do NOT kill the whole run.
    if (-not $igWarned -and $joined -match "Active Instagram scrape failed") {
        $igWarned = $true
        Write-Status -State "degraded" -Detail "instagram scrape failed (port 9222 likely down)" -Streak 0
        & $notify -Title "Quant Pipeline Warning" -Message "Instagram pass failed (browser port 9222 unreachable). Other sources continue." 2>$null
        Write-Output "SUPERVISOR-NOTIFY ig_failed"
    }
    if (-not $consensusWarned -and $joined -match "Consensus scrape failed") {
        $consensusWarned = $true
        & $notify -Title "Quant Pipeline Warning" -Message "Consensus scrape (Glassdoor/G2/LinkedIn) failed this pass." 2>$null
        Write-Output "SUPERVISOR-NOTIFY consensus_failed"
    }

    # MILESTONE: the data quantity/validity report means all passes finished.
    if (-not $milestoneShown -and $joined -match "PIPELINE DATA QUANTITY REPORT") {
        $milestoneShown = $true
        Write-Status -State "milestone" -Detail "data quantity + validity report generated" -Streak 0
        & $notify -Title "Quant Pipeline Milestone" -Message "Active IG + consensus + sentinel passes done. Stats report generated." 2>$null
        Write-Output "SUPERVISOR-MILESTONE report_generated"
    }

    # CONSENSUS health: parse the machine-readable evidence line when present.
    if ($joined -match "\[CONSENSUS-EVIDENCE\] ticker=(\S+) usable=(\d+) reviews=(\d+) flags=(\S+) score=([-\d.]+)") {
        $ceTick = $Matches[1]; $ceUse = [int]$Matches[2]; $ceRev = [int]$Matches[3]
        $ceFlags = $Matches[4]; $ceScore = [float]$Matches[5]
        $ceState = if ($LiveConsensus -and $ceUse -eq 0 -and $ceRev -eq 0) { "zero_evidence" } elseif ($LiveConsensus) { "live_ok" } else { "offline" }
        Write-ConsensusStatus -Ticker $ceTick -Usable $ceUse -Reviews $ceRev -Flags $ceFlags -Score $ceScore -State $ceState
        if ($ceState -eq "zero_evidence") {
            & $notify -Title "Quant Consensus ISSUE" -Message "Live consensus returned ZERO evidence ($ceRev reviews, 0 usable) - plan F1. Check logs." 2>$null
            Write-Output "SUPERVISOR-NOTIFY consensus_zero_evidence"
        }
    }

    # Timeout guard
    if (((Get-Date) - $start).TotalMinutes -gt $MaxWaitMinutes) {
        Write-Status -State "timeout" -Detail "watch exceeded $MaxWaitMinutes minutes" -Streak $issueStreak
        Write-Output "SUPERVISOR-TIMEOUT"
        exit 2
    }
}

$proc.WaitForExit()
$full = if (Test-Path $outLog) { Get-Content $outLog -Raw } else { "" }
$errFull = if (Test-Path $errLog) { Get-Content $errLog -Raw } else { "" }
$hasReport = $full -match "PIPELINE DATA QUANTITY REPORT"
$hasTraceback = ($full + $errFull) -match "Traceback|FAIL-HARD|InstagramChallengeDetected"
$code = -1
try { $code = $proc.ExitCode } catch { $code = -1 }
if ($hasReport -and -not $hasTraceback) { $state = "done_ok"; $code = 0 }
elseif ($hasTraceback) { $state = "done_error"; $code = 1 }
else { $state = "done_error"; $code = $code }

# Consensus evidence: parse the LAST evidence line from the FULL log (the
# tail-window poll can miss it when the report scrolls it out).
$ceM = [regex]::Matches($full, "\[CONSENSUS-EVIDENCE\] ticker=(\S+) usable=(\d+) reviews=(\d+) flags=(\S+) score=([-\d.]+)")
if ($ceM.Count -gt 0) {
    $lastCe = $ceM[$ceM.Count - 1]
    $ceTick = $lastCe.Groups[1].Value
    $ceUse = [int]$lastCe.Groups[2].Value
    $ceRev = [int]$lastCe.Groups[3].Value
    $ceFlags = $lastCe.Groups[4].Value
    $ceScore = [float]$lastCe.Groups[5].Value
    $ceState = if ($LiveConsensus -and $ceUse -eq 0 -and $ceRev -eq 0) { "zero_evidence" } elseif ($LiveConsensus) { "live_ok" } else { "offline" }
    Write-ConsensusStatus -Ticker $ceTick -Usable $ceUse -Reviews $ceRev -Flags $ceFlags -Score $ceScore -State $ceState
}
Write-Status -State $state -Detail "process exited (code=$code, report=$hasReport, traceback=$hasTraceback)" -Streak $issueStreak
if ($state -eq "done_ok") {
    if ($LiveConsensus) {
        $csPath = Join-Path $root $ConsensusStatusFile
        if (Test-Path $csPath) {
            $cs = Get-Content $csPath -Raw | ConvertFrom-Json
            if ($cs.state -eq "zero_evidence") {
                & $notify -Title "Quant Consensus ISSUE" -Message "Live consensus finished with ZERO evidence - plan F1 rejects. See $LogFile" 2>$null
            } else {
                & $notify -Title "Quant Consensus Milestone" -Message "Live consensus OK: $($cs.ticker) usable=$($cs.usable_sources) reviews=$($cs.total_reviews) score=$($cs.score)" 2>$null
            }
        }
    }
    & $notify -Title "Quant Pipeline Done" -Message "Pipeline completed cleanly (report generated, no traceback). See $LogFile" 2>$null
} else {
    & $notify -Title "Quant Pipeline Issue" -Message "Pipeline ended with issues (code $code). Check $LogFile" 2>$null
}
Write-Output "SUPERVISOR-DONE code=$code state=$state"
exit 0