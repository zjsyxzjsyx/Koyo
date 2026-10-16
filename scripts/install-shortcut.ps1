# 在桌面生成“知秋工作台”快捷方式（图标存到 %LOCALAPPDATA%\zhiqiu\zhiqiu-web-<指纹>.ico）。
# 可重复运行：已有的快捷方式和图标会被覆盖。用法：powershell -ExecutionPolicy Bypass -File scripts\install-shortcut.ps1
param([string]$Db = '')

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

$root = Split-Path $PSScriptRoot
$launcher = Join-Path $PSScriptRoot 'zhiqiu-web.ps1'
$iconDir = Join-Path $env:LOCALAPPDATA 'zhiqiu'
New-Item -ItemType Directory -Force $iconDir | Out-Null

function New-IconPng([int]$size) {
    # 与网页图标（dashboard.html 的 favicon）一致：琥珀色圆角方块 + 白色枫叶，坐标按 64×64 缩放。
    $bmp = New-Object System.Drawing.Bitmap $size, $size
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = 'AntiAlias'
    $r = [Math]::Max(2, [int]($size * 14 / 64))
    $d = 2 * $r
    $shape = New-Object System.Drawing.Drawing2D.GraphicsPath
    $shape.AddArc(0, 0, $d, $d, 180, 90)
    $shape.AddArc($size - $d - 1, 0, $d, $d, 270, 90)
    $shape.AddArc($size - $d - 1, $size - $d - 1, $d, $d, 0, 90)
    $shape.AddArc(0, $size - $d - 1, $d, $d, 90, 90)
    $shape.CloseFigure()
    $g.FillPath((New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(0xBA, 0x75, 0x17))), $shape)
    $k = $size / 64
    $leaf = @(32,7, 36.5,16, 41,14, 39,26, 46,19, 48,23.5, 55,22, 52,30, 55,32, 44,40, 45.5,44, 34,42.5,
              30,42.5, 18.5,44, 20,40, 9,32, 12,30, 9,22, 16,23.5, 18,19, 25,26, 23,14, 27.5,16)
    $points = for ($i = 0; $i -lt $leaf.Count; $i += 2) {
        New-Object System.Drawing.PointF ($leaf[$i] * $k), ($leaf[$i + 1] * $k)
    }
    $g.FillPolygon([System.Drawing.Brushes]::White, [System.Drawing.PointF[]]$points)
    $stem = New-Object System.Drawing.Pen ([System.Drawing.Color]::White), ([Math]::Max(1, 3.5 * $k))
    $stem.StartCap = 'Round'
    $stem.EndCap = 'Round'
    $g.DrawLine($stem, 32 * $k, 42 * $k, 32 * $k, 53 * $k)
    $g.Dispose()
    $stream = New-Object System.IO.MemoryStream
    $bmp.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    return ,$stream.ToArray()
}

# ICO 文件：头部 + 目录项 + 各尺寸的 PNG 数据。
$sizes = 16, 24, 32, 48, 64, 256
$images = foreach ($s in $sizes) { ,(New-IconPng $s) }
$buffer = New-Object System.IO.MemoryStream
$file = New-Object System.IO.BinaryWriter $buffer
$file.Write([uint16]0); $file.Write([uint16]1); $file.Write([uint16]$sizes.Count)
$offset = 6 + 16 * $sizes.Count
for ($i = 0; $i -lt $sizes.Count; $i++) {
    $side = if ($sizes[$i] -ge 256) { 0 } else { $sizes[$i] }
    $file.Write([byte]$side); $file.Write([byte]$side); $file.Write([byte]0); $file.Write([byte]0)
    $file.Write([uint16]1); $file.Write([uint16]32)
    $file.Write([uint32]$images[$i].Length); $file.Write([uint32]$offset)
    $offset += $images[$i].Length
}
foreach ($png in $images) { $file.Write($png) }
$file.Flush()
$bytes = $buffer.ToArray()
# 资源管理器按路径缓存图标：文件名带上内容指纹，图标变了路径就变，桌面才会换新图标。
$sha = [System.Security.Cryptography.SHA256]::Create()
$digest = -join ($sha.ComputeHash($bytes)[0..3] | ForEach-Object { $_.ToString('x2') })
$icon = Join-Path $iconDir "zhiqiu-web-$digest.ico"
Get-ChildItem $iconDir -Filter 'zhiqiu-web*.ico' | Where-Object FullName -ne $icon | Remove-Item
[System.IO.File]::WriteAllBytes($icon, $bytes)

$arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`""
if ($Db) { $arguments += " -Db `"$Db`"" }
$link = Join-Path ([Environment]::GetFolderPath('Desktop')) '知秋工作台.lnk'
$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($link)
$shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = "$icon,0"
$shortcut.WindowStyle = 7
$shortcut.Description = '打开知秋网页工作台'
$shortcut.Save()
# 通知资源管理器刷新图标。
Add-Type -Namespace Zhiqiu -Name Shell -MemberDefinition '[DllImport("shell32.dll")] public static extern void SHChangeNotify(int e, uint f, System.IntPtr a, System.IntPtr b);'
[Zhiqiu.Shell]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)
Write-Output "已生成快捷方式：$link"
