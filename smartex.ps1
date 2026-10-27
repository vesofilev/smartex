# Start, stop or inspect the smartex server on Windows; the PowerShell version of smartex.sh.
#   .\smartex.ps1 start [DIR ...]       serve these LaTeX project folders and the ones served before (default
#                                       port 8765, or SMARTEX_PORT); the list is kept in ~\.smartex_projects
#   .\smartex.ps1 stop | status | url
# If Windows refuses to run scripts, use: powershell -ExecutionPolicy Bypass -File .\smartex.ps1 start DIR
# Settings can live in ~\.smartex.env, one NAME=value per line as for smartex.sh (# starts a comment):
#   SMARTEX_PORT=8765                      the loopback port (browser, CLI, MCP)
#   SMARTEX_PROJECTS_DIR=~/papers          where the New project button makes folders (default ~\smartex-projects)
#   SMARTEX_TEXBIN=C:\texlive\2026\bin\windows   the folder with latexmk, if it is not on PATH
#   SMARTEX_CLAUDE=C:\path\to\claude.exe   Claude Code for the assistant, if it is not on PATH
#   SMARTEX_PYTHON=C:\path\to\python.exe   the Python to use (default: py -3, then python, then python3)
#   SMARTEX_PUBLIC, SMARTEX_ALLOW_FROM, SMARTEX_PUBLIC_URL   a reverse-proxy listener, as in smartex.sh
# Written for Windows PowerShell 5.1 and PowerShell 7; keep this file ASCII (5.1 reads it in the ANSI code page).
param(
    [Parameter(Position = 0)][string]$Command,
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Dirs
)
$ErrorActionPreference = 'Stop'
$Here = $PSScriptRoot
$OnWindows = $env:OS -eq 'Windows_NT'
$PidFile = Join-Path $HOME '.smartex.pid'
$Log = Join-Path $HOME '.smartex.log'            # what the server prints, including the address to open
$ErrLog = Join-Path $HOME '.smartex.err.log'     # requests and errors
$Registry = Join-Path $HOME '.smartex_projects'
$Usage = @'
usage: .\smartex.ps1 start [DIR ...]   serve these project folders and the ones served before
       .\smartex.ps1 stop | status | url
'@

# ~\.smartex.env: NAME=value lines, quotes and trailing comments allowed, ~ at the start means the home folder
$Settings = @{}
$EnvFile = Join-Path $HOME '.smartex.env'
if (Test-Path -LiteralPath $EnvFile) {
    foreach ($line in Get-Content -LiteralPath $EnvFile) {
        if ($line -notmatch '^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { continue }
        $name = $Matches[1]; $value = $Matches[2].Trim()
        if ($value -match '^"([^"]*)"' -or $value -match "^'([^']*)'") { $value = $Matches[1] }
        else { $value = ($value -replace '\s+#.*$', '').Trim() }
        if ($value -like '~*') { $value = $HOME + $value.Substring(1) }
        $Settings[$name] = $value
    }
}
function Setting([string]$name, [string]$default = '') {
    if ($Settings.ContainsKey($name)) { return $Settings[$name] }
    $v = [Environment]::GetEnvironmentVariable($name)
    if ($v) { return $v } else { return $default }
}
$Port = Setting 'SMARTEX_PORT' '8765'

function Get-Server {
    # the server process, if the PID file names one that still runs Python
    if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
    $id = 0
    if (-not [int]::TryParse((Get-Content -LiteralPath $PidFile -Raw).Trim(), [ref]$id)) { return $null }
    $p = Get-Process -Id $id -ErrorAction SilentlyContinue
    if ($p -and $p.ProcessName -match '^(py|pythonw?|python3(\.\d+)?)$') { return $p }
    return $null
}

function Find-Python {
    # the command (with its arguments) of a Python 3.8 or newer; the Microsoft Store stub fails the check
    $tries = @('py -3', 'python', 'python3')
    $own = Setting 'SMARTEX_PYTHON'
    if ($own) { $tries = @($own) }
    foreach ($t in $tries) {
        $words = @($t -split ' ')
        if ($own -and (Test-Path -LiteralPath $own)) { $words = @($own) }
        $cmd = Get-Command $words[0] -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $cmd) { continue }
        $rest = @($words | Select-Object -Skip 1)
        $ErrorActionPreference = 'Continue'
        & $cmd.Source @rest -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>$null | Out-Null
        $ok = $LASTEXITCODE -eq 0
        $ErrorActionPreference = 'Stop'
        if ($ok) { return @($cmd.Source) + $rest }        # unrolled here; the caller collects it with @()
    }
}

function Quote-Arg([string]$a) {
    # one command-line argument as Windows programs split them (Start-Process joins its arguments unquoted)
    if ($a -ne '' -and $a -notmatch '[\s"]') { return $a }
    return '"' + (($a -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
}

function Has-Registered {
    if (-not (Test-Path -LiteralPath $Registry)) { return $false }
    return [bool](Get-Content -LiteralPath $Registry | Where-Object { $_.Trim() -and -not $_.TrimStart().StartsWith('#') })
}

switch ($Command) {
    'start' {
        if (-not $Dirs -and -not (Has-Registered)) {
            Write-Output "usage: .\smartex.ps1 start DIR [DIR ...]   (no projects in $Registry yet)"; exit 1
        }
        $running = Get-Server
        if ($running) { Write-Output "already running (pid $($running.Id))"; exit 0 }
        $py = @(Find-Python)
        if ($py.Count -eq 0) { Write-Output 'Python 3.8 or newer was not found: install it from python.org, or set SMARTEX_PYTHON'; exit 1 }

        # -u: print at once, so the address shows up in the log; -X utf8: read and write files as UTF-8
        $argv = @($py | Select-Object -Skip 1) + @('-u', '-X', 'utf8', (Join-Path $Here 'server.py'), '--registry', $Registry)
        foreach ($d in $Dirs) {
            # absolute paths, since the server does not start in this folder
            if (Test-Path -LiteralPath $d -PathType Container) { $d = (Resolve-Path -LiteralPath $d).ProviderPath }
            $argv += @('--root', $d)
        }
        $projectsDir = Setting 'SMARTEX_PROJECTS_DIR'
        if ($projectsDir) { $argv += @('--projects-dir', $projectsDir) }
        $public = Setting 'SMARTEX_PUBLIC'
        if ($public) {
            $argv += @('--public', $public)
            foreach ($a in ((Setting 'SMARTEX_ALLOW_FROM') -split '\s+' | Where-Object { $_ })) { $argv += @('--allow-from', $a) }
        }
        $argv += @('--port', $Port)

        # keep the previous run's logs
        foreach ($f in @($Log, $ErrLog)) { if (Test-Path -LiteralPath $f) { Move-Item -Force -LiteralPath $f -Destination "$f.1" } }

        # the server reads SMARTEX_TEXBIN, SMARTEX_CLAUDE and the like from its environment: pass the settings
        # on, and put this window's environment back afterwards
        $saved = @{}
        foreach ($k in $Settings.Keys) {
            $saved[$k] = [Environment]::GetEnvironmentVariable($k)
            [Environment]::SetEnvironmentVariable($k, $Settings[$k])
        }
        try {
            $opts = @{ FilePath = $py[0]; ArgumentList = (($argv | ForEach-Object { Quote-Arg $_ }) -join ' ')
                       WorkingDirectory = $HOME; RedirectStandardOutput = $Log; RedirectStandardError = $ErrLog; PassThru = $true }
            if ($OnWindows) { $opts.WindowStyle = 'Hidden' }      # keeps running after this window is closed
            $p = Start-Process @opts
        } finally {
            foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) }
        }
        Set-Content -LiteralPath $PidFile -Value $p.Id

        $open = $null
        for ($i = 0; $i -lt 30 -and -not $open -and -not $p.HasExited; $i++) {
            Start-Sleep -Milliseconds 500
            $open = Select-String -LiteralPath $Log -Pattern '^open: (.*)' -ErrorAction SilentlyContinue | Select-Object -First 1
        }
        if ($open -and -not $p.HasExited) {
            Write-Output "started; open: $($open.Matches[0].Groups[1].Value)"
        } else {
            Write-Output 'failed to start:'
            Get-Content -LiteralPath $Log, $ErrLog -ErrorAction SilentlyContinue
            if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
            Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue
            exit 1
        }
    }
    'stop' {
        $p = Get-Server
        if (-not $p) { Write-Output 'not running'; Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue; break }
        if ($OnWindows) {
            # /T: also the assistants (Claude Code) that the server started; Windows has no gentle stop for them
            & taskkill.exe /PID $p.Id /T /F | Out-Null
        } else {
            Stop-Process -Id $p.Id
        }
        if (-not $p.WaitForExit(15000)) { Write-Output "still stopping (pid $($p.Id)); try again in a moment"; exit 1 }
        Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue
        Write-Output 'stopped'
    }
    'status' {
        $p = Get-Server
        if ($p) {
            $extra = ''
            $public = Setting 'SMARTEX_PUBLIC'
            if ($public) { $extra = " and $public (proxy)" }
            Write-Output "running (pid $($p.Id)) on 127.0.0.1:$Port$extra"
        } else { Write-Output 'not running' }
    }
    'url' {
        $tokenFile = Join-Path $HOME '.smartex_token'
        if (-not (Test-Path -LiteralPath $tokenFile)) { Write-Output 'no token yet: start the server once'; exit 1 }
        $token = (Get-Content -LiteralPath $tokenFile -Raw).Trim()
        Write-Output "http://localhost:$Port/?token=$token"
        $publicUrl = Setting 'SMARTEX_PUBLIC_URL'
        if ($publicUrl) { Write-Output "$publicUrl    (sign in with the token in ~\.smartex_token)" }
    }
    default { Write-Output $Usage; exit 1 }
}
