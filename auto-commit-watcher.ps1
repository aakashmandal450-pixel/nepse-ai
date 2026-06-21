# auto-commit-watcher.ps1
#
# Watches D:\Stockpile for code changes, waits for things to settle,
# shows you exactly what changed, and ONLY commits + pushes to GitHub
# after you type 'y'. Nothing is ever pushed silently.
#
# Run it from a PowerShell window and leave that window open:
#   powershell -ExecutionPolicy Bypass -File .\auto-commit-watcher.ps1
#
# Stop it any time with Ctrl+C.

$RepoPath        = "D:\Stockpile"
$Branch          = "main"
$DebounceSeconds = 8     # how long to wait after the last file save before asking you

# Paths the watcher should never react to, even if something touches them
# (these should also be in your .gitignore, but this is a second layer of safety)
$IgnorePatterns = @(
    '\\\.git\\',
    '\\__pycache__\\',
    '\\\.venv\\',
    '\\venv\\',
    '\\logs\\',
    '\\exports\\',
    '\\models\\',
    '\\\.vscode\\',
    'nepse_ai\.db$',
    'session[ _]pooler\.txt$',
    'secrets\.toml$',
    '\.pyc$',
    '\.log$'
)

if (-not (Test-Path $RepoPath)) {
    Write-Host "Repo path not found: $RepoPath" -ForegroundColor Red
    exit 1
}
Set-Location $RepoPath

if (-not (Test-Path (Join-Path $RepoPath ".git"))) {
    Write-Host "$RepoPath is not a git repository." -ForegroundColor Red
    exit 1
}

$watcher = New-Object System.IO.FileSystemWatcher
$watcher.Path = $RepoPath
$watcher.IncludeSubdirectories = $true
$watcher.NotifyFilter = [System.IO.NotifyFilters]'LastWrite, FileName, DirectoryName'

$global:PendingChange = $false

$onChange = {
    $path = $Event.SourceEventArgs.FullPath
    foreach ($pattern in $using:IgnorePatterns) {
        if ($path -match $pattern) { return }
    }
    $global:PendingChange = $true
}

$handlers = @()
$handlers += Register-ObjectEvent $watcher Changed -Action $onChange
$handlers += Register-ObjectEvent $watcher Created -Action $onChange
$handlers += Register-ObjectEvent $watcher Deleted -Action $onChange
$handlers += Register-ObjectEvent $watcher Renamed -Action $onChange
$watcher.EnableRaisingEvents = $true

Write-Host "================================================================" -ForegroundColor Cyan
Write-Host " Watching: $RepoPath" -ForegroundColor Cyan
Write-Host " Push target: origin/$Branch" -ForegroundColor Cyan
Write-Host " Nothing is pushed without your confirmation. Ctrl+C to stop." -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan

try {
    while ($true) {
        Start-Sleep -Seconds 2

        if ($global:PendingChange) {
            # Debounce: wait until saves have settled before asking
            Start-Sleep -Seconds $DebounceSeconds
            $global:PendingChange = $false

            $status = git status --porcelain
            if (-not $status) { continue }

            Write-Host "`n================ Changes detected =================" -ForegroundColor Yellow
            git status --short
            Write-Host "`n--- Diff ---" -ForegroundColor Yellow
            git --no-pager diff
            git --no-pager diff --cached
            Write-Host "=====================================================" -ForegroundColor Yellow

            $confirm = Read-Host "`nCommit and push these changes to '$Branch'? (y/N)"
            if ($confirm -eq 'y') {
                $msg = Read-Host "Commit message (Enter for auto-generated)"
                if ([string]::IsNullOrWhiteSpace($msg)) {
                    $msg = "Auto-commit: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
                }
                git add -A
                git commit -m $msg
                git push origin $Branch
                Write-Host "Pushed to $Branch." -ForegroundColor Green
            } else {
                Write-Host "Skipped — will ask again on the next change." -ForegroundColor DarkGray
            }
        }
    }
}
finally {
    $handlers | ForEach-Object { Unregister-Event -SourceIdentifier $_.Name -ErrorAction SilentlyContinue }
    $watcher.Dispose()
}
