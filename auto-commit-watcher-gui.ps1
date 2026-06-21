# auto-commit-watcher-gui.ps1
#
# Watches D:\Stockpile for code changes. When things settle, it opens
# Git GUI so you can visually review the diff and click Commit. Once
# you close Git GUI, it checks whether you actually committed anything
# and asks you to confirm before pushing to GitHub.
#
# Run it from a PowerShell window and leave that window open:
#   powershell -ExecutionPolicy Bypass -File .\auto-commit-watcher-gui.ps1
#
# Stop it any time with Ctrl+C.

$RepoPath        = "D:\Stockpile"
$Branch          = "main"
$DebounceSeconds = 8     # how long to wait after the last file save before opening Git GUI

# Paths the watcher should never react to (mirrors .gitignore as a second safety layer)
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
Write-Host " Git GUI opens for review when changes settle. Push needs your" -ForegroundColor Cyan
Write-Host " confirmation. Ctrl+C to stop." -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan

try {
    while ($true) {
        Start-Sleep -Seconds 2

        if ($global:PendingChange) {
            # Debounce: wait until saves have settled before opening Git GUI
            Start-Sleep -Seconds $DebounceSeconds
            $global:PendingChange = $false

            $status = git status --porcelain
            if (-not $status) { continue }

            Write-Host "`n[$(Get-Date -Format 'HH:mm:ss')] Changes detected — opening Git GUI..." -ForegroundColor Yellow

            # Opens Git GUI and waits until you close it
            Start-Process -FilePath "git" -ArgumentList "gui" -WorkingDirectory $RepoPath -Wait

            # After Git GUI closes, see if a commit was actually made
            git fetch origin $Branch *> $null
            $ahead = (git rev-list --count "origin/$Branch..HEAD" 2>$null)

            if ($ahead -and [int]$ahead -gt 0) {
                $confirm = Read-Host "`n$ahead new commit(s) ready. Push to origin/$Branch now? (y/N)"
                if ($confirm -eq 'y') {
                    git push origin $Branch
                    Write-Host "Pushed to $Branch." -ForegroundColor Green
                } else {
                    Write-Host "Not pushed. Run 'git push origin $Branch' manually when ready." -ForegroundColor DarkGray
                }
            } else {
                Write-Host "No new commits were made — nothing to push." -ForegroundColor DarkGray
            }
        }
    }
}
finally {
    $handlers | ForEach-Object { Unregister-Event -SourceIdentifier $_.Name -ErrorAction SilentlyContinue }
    $watcher.Dispose()
}
