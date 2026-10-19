# 打开知秋网页工作台：服务没在运行就先启动（最小化的“知秋工作台服务”窗口，关掉它即停止），
# 再用 Edge 的独立窗口打开；没有 Edge 时用默认浏览器。服务已在运行时只打开窗口。
# 端口和数据库不传时取配置（zhiqiu config get web.port、web.db）；配置里没写数据库、项目目录里又有
# state.sqlite 时继续用它（9.0 之前快捷方式一直用这个库）。
param(
    [int]$Port = 0,
    [string]$Db = ''
)

$root = Split-Path $PSScriptRoot
$title = '知秋工作台'

function Show-Message([string]$text) {
    (New-Object -ComObject WScript.Shell).Popup($text, 0, $title, 48) | Out-Null
}

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe' }
if (-not (Test-Path $uv)) {
    Show-Message '找不到 uv，请先安装 uv 并在项目目录运行 uv sync。'
    exit 1
}

function Get-Setting([string]$name) {
    Push-Location $root
    try { return (& $uv run --no-sync zhiqiu config get $name 2>$null | Select-Object -First 1) } finally { Pop-Location }
}

if (-not $Port) {
    $Port = [int](Get-Setting 'web.port')
    if (-not $Port) { $Port = 8765 }
}
if (-not $Db) {
    $projectDb = Join-Path $root 'state.sqlite'
    if (-not (Get-Setting 'web.db') -and (Test-Path $projectDb)) { $Db = $projectDb }
}
$url = "http://127.0.0.1:$Port/"

function Get-Mode {
    # workbench：工作台已在运行；dashboard：只读看板占着端口；other：别的程序；$null：没有服务。
    try {
        $content = (Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 2).Content
    } catch {
        if ($_.Exception.Response) { return 'other' }
        return $null
    }
    if ($content -match 'name="zhiqiu-mode" content="workbench"') { return 'workbench' }
    if ($content -match 'data-view="live"') { return 'dashboard' }
    return 'other'
}

function Test-Running([int]$id) {
    # cmd 窗口还在、且其中的 uv 仍在运行；失败时 cmd 停在 pause，只剩 conhost 子进程。
    if (-not (Get-Process -Id $id -ErrorAction SilentlyContinue)) { return $false }
    return [bool](Get-CimInstance Win32_Process -Filter "ParentProcessId=$id AND Name='uv.exe'")
}

$mode = Get-Mode
if ($mode -eq 'dashboard') {
    Show-Message "端口 $Port 上运行的是只读看板（zhiqiu dashboard），请先关掉它再打开工作台。"
    exit 1
}
if ($mode -eq 'other') {
    Show-Message "端口 $Port 已被其他程序占用，工作台无法启动。"
    exit 1
}
if (-not $mode) {
    $dbArgument = if ($Db) { "--db `"$Db`" " } else { '' }
    # --no-open：窗口由本脚本打开（Edge 独立窗口），不再让服务另开一个浏览器标签。
    $command = "title 知秋工作台服务（关闭本窗口即停止） & `"$uv`" run zhiqiu $dbArgument" + "web --port $Port --no-open" +
               " || (echo. & echo 工作台没有启动，原因见上方。 & pause)"
    $server = Start-Process cmd.exe -ArgumentList '/c', $command -WorkingDirectory $root -WindowStyle Minimized -PassThru
    $deadline = (Get-Date).AddSeconds(60)
    $misses = 0
    while ((Get-Mode) -ne 'workbench') {
        if ((Get-Date) -gt $deadline) {
            Show-Message '工作台 60 秒内没有启动完成，请查看任务栏里的“知秋工作台服务”窗口。'
            exit 1
        }
        # 连续两次看不到 uv 才算失败，避开 cmd 刚启动、子进程还没出现的瞬间。
        if (Test-Running $server.Id) { $misses = 0 } else { $misses++ }
        if ($misses -ge 2) {
            Show-Message '工作台没有启动，原因见任务栏里的“知秋工作台服务”窗口。'
            exit 1
        }
        Start-Sleep -Milliseconds 500
    }
}

$edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
          "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
if ($edge) {
    Start-Process $edge "--app=$url"
} else {
    Start-Process $url
}
