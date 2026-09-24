# J.A.R.V.I.S. installer for Windows.
#
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/dylanleitenberg-tech/JARVIS/main/install.ps1 | iex"
#
# Installs into %LOCALAPPDATA%\JARVIS (no administrator rights): the code, a
# private Python from uv, and Start-menu and desktop shortcuts. Running it
# again updates the code and keeps your settings, logs and caches. Windows
# needs no permission for keyboard and mouse control; camera and microphone
# are asked for by JARVIS itself, in its setup panel, when it opens.
#
# Environment variables: JARVIS_HOME (install location), JARVIS_SOURCE
# (install from a local checkout), JARVIS_BRANCH (default main),
# JARVIS_NO_SHORTCUTS=1, JARVIS_NO_LAUNCH=1, JARVIS_WITH_STEP=1 (CadQuery for
# STEP files, about 1 GB), JARVIS_UNINSTALL=1.
# Works on the Windows PowerShell 5.1 that ships with Windows 10 and 11.

function Install-Jarvis {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'     # Invoke-WebRequest is 10x slower with the bar

    $Repo = 'dylanleitenberg-tech/JARVIS'
    $Branch = if ($env:JARVIS_BRANCH) { $env:JARVIS_BRANCH } else { 'main' }
    $Dest = if ($env:JARVIS_HOME) { $env:JARVIS_HOME } else { Join-Path $env:LOCALAPPDATA 'JARVIS' }
    $StartMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'J.A.R.V.I.S..lnk'
    $DesktopLink = Join-Path ([Environment]::GetFolderPath('Desktop')) 'J.A.R.V.I.S..lnk'

    function Say($text) { Write-Host "  $text" }
    function Step($text) { Write-Host ""; Write-Host "  > $text" -ForegroundColor Cyan }
    function Die($text) { throw "J.A.R.V.I.S. install failed: $text" }

    # ------------------------------------------------------------------ uninstall
    if ($env:JARVIS_UNINSTALL -eq '1') {
        Step 'Removing J.A.R.V.I.S.'
        Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
            Where-Object { $_.CommandLine -like "*$Dest*" } |     # this install's only
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Remove-Item -Force -ErrorAction SilentlyContinue $StartMenu, $DesktopLink
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Dest
        Say "removed $Dest"
        Write-Host ""; Say 'Done. Your model files were not touched.'; Write-Host ""
        return
    }

    Write-Host ""; Say "J.A.R.V.I.S. - installing into $Dest"

    # ----------------------------------------------------------------------- code
    Step 'Getting the code'
    $Tmp = Join-Path ([IO.Path]::GetTempPath()) ("jarvis-" + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force $Tmp | Out-Null
    try {
        if ($env:JARVIS_SOURCE) {
            $Src = $env:JARVIS_SOURCE
            Say "from $Src"
        } else {
            $Zip = Join-Path $Tmp 'src.zip'
            Invoke-WebRequest -UseBasicParsing "https://codeload.github.com/$Repo/zip/refs/heads/$Branch" -OutFile $Zip
            Expand-Archive $Zip -DestinationPath $Tmp -Force
            $Src = (Get-ChildItem $Tmp -Directory | Select-Object -First 1).FullName
        }
        if (-not (Test-Path (Join-Path $Src 'jarvis\main.py'))) { Die "$Src does not look like J.A.R.V.I.S." }

        New-Item -ItemType Directory -Force $Dest | Out-Null
        # Code is replaced; what belongs to the person is kept: settings, logs, the
        # browser profile holding the microphone grant, the Python, build caches.
        foreach ($item in 'jarvis', 'web', 'bridge', 'tests', 'packaging', 'requirements.txt', 'README.md', 'install.sh', 'install.ps1') {
            $from = Join-Path $Src $item
            if (Test-Path $from) {
                $to = Join-Path $Dest $item
                if (Test-Path $to) { Remove-Item -Recurse -Force $to }
                Copy-Item -Recurse -Force $from $to
            }
        }
        New-Item -ItemType Directory -Force (Join-Path $Dest 'logs') | Out-Null

        # --------------------------------------------------------------- python
        Step 'Setting up Python'
        $Uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
        if (-not $Uv) {
            $Uv = Join-Path $Dest '.uv\uv.exe'
            if (-not (Test-Path $Uv)) { $Uv = Join-Path $Dest '.uv\bin\uv.exe' }
            if (-not (Test-Path $Uv)) {
                Say "installing uv (a Python installer) into $Dest\.uv"
                $env:UV_INSTALL_DIR = Join-Path $Dest '.uv'
                $env:UV_NO_MODIFY_PATH = '1'
                $env:INSTALLER_NO_MODIFY_PATH = '1'
                Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression | Out-Null
                $Uv = @((Join-Path $Dest '.uv\uv.exe'), (Join-Path $Dest '.uv\bin\uv.exe')) |
                    Where-Object { Test-Path $_ } | Select-Object -First 1
            }
            if (-not (Test-Path $Uv)) { Die 'could not install uv' }
        }
        $Py = Join-Path $Dest '.venv\Scripts\python.exe'
        $PyW = Join-Path $Dest '.venv\Scripts\pythonw.exe'
        if (-not (Test-Path $Py)) {
            & $Uv venv --quiet --python 3.12 (Join-Path $Dest '.venv')
            if ($LASTEXITCODE -ne 0) { Die 'could not create the Python environment' }
        }

        Step 'Installing packages (a few minutes the first time)'
        $Req = Get-Content (Join-Path $Dest 'requirements.txt')
        $Core = Join-Path $Tmp 'core.txt'
        $Req | Where-Object { $_ -notmatch '^mediapipe' } | Set-Content $Core
        & $Uv pip install --quiet --python $Py -r $Core
        if ($LASTEXITCODE -ne 0) { Die 'package install failed' }
        $Vision = $true
        & $Uv pip install --quiet --python $Py ($Req | Where-Object { $_ -match '^mediapipe' })
        if ($LASTEXITCODE -ne 0) {
            $Vision = $false
            Say 'hand tracking (MediaPipe) is not available for this machine; voice and typing still work'
        }

        if ($env:JARVIS_WITH_STEP -eq '1') {
            Step 'Installing CadQuery for STEP files'
            $StepPy = Join-Path $Dest '.step-env\Scripts\python.exe'
            if (-not (Test-Path $StepPy)) { & $Uv venv --quiet --python 3.12 (Join-Path $Dest '.step-env') }
            & $Uv pip install --quiet --python $StepPy cadquery
            if ($LASTEXITCODE -ne 0) { Say 'CadQuery did not install; STEP files will be listed but not drawn' }
        }

        $Config = Join-Path $Dest 'jarvis.json'
        if (-not (Test-Path $Config) -and -not $Vision) {
            '{ "vision": { "enabled": false } }' | Set-Content $Config
        }

        # ------------------------------------------------------------ shortcuts
        if ($env:JARVIS_NO_SHORTCUTS -ne '1') {
            Step 'Adding shortcuts'
            $Shell = New-Object -ComObject WScript.Shell
            foreach ($link in $StartMenu, $DesktopLink) {
                $s = $Shell.CreateShortcut($link)
                $s.TargetPath = $PyW                   # pythonw: no console window
                $s.Arguments = '-m jarvis.main --log'
                $s.WorkingDirectory = $Dest
                $s.IconLocation = Join-Path $Dest 'packaging\windows\jarvis.ico'
                $s.Description = 'Voice and hand-gesture assistant'
                $s.Save()
            }
            Say 'Start menu and desktop: J.A.R.V.I.S.'
        }

        $Browsers = @(
            "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
            "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
            "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
            "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
            "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe")
        if (-not ($Browsers | Where-Object { $_ -and (Test-Path $_) })) {
            Say 'For voice, install Google Chrome or Microsoft Edge.'
        }

        Write-Host ""; Write-Host "  Installed." -ForegroundColor Green
        Say 'It asks for camera and microphone access when it opens.'
        Say "Remove it any time: set JARVIS_UNINSTALL=1 and run install.ps1 again."

        # --------------------------------------------------------------- launch
        if ($env:JARVIS_NO_LAUNCH -ne '1') {
            Step 'Starting J.A.R.V.I.S.'
            Start-Process -FilePath $PyW -ArgumentList '-m', 'jarvis.main', '--log' -WorkingDirectory $Dest
        }
        Write-Host ""
    }
    finally {
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Tmp
    }
}

# A function, so that when this runs through `irm | iex` a failure or an early
# finish ends the install, never the PowerShell window it was typed into.
Install-Jarvis
