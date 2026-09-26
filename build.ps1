# Build one Windows executable and replace the copy in the project root.
# PyInstaller's intermediate files always stay in the same ignored build/ tree.
$ErrorActionPreference = 'Stop'

$projectRoot = $PSScriptRoot
$targetExe = Join-Path $projectRoot 'Kinoko7Danmaku.exe'
$buildRoot = Join-Path $projectRoot 'build'
$packageDir = Join-Path $buildRoot 'package'
$workDir = Join-Path $buildRoot 'pyinstaller'
$builtExe = Join-Path $packageDir 'Kinoko7Danmaku.exe'

$running = Get-Process -Name 'Kinoko7Danmaku' -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -ieq $targetExe }
if ($running) {
    throw '请先退出正在运行的项目根目录 Kinoko7Danmaku.exe，再重新打包。'
}

Push-Location $projectRoot
try {
    & uv run --locked pyinstaller --noconfirm --clean --distpath $packageDir --workpath $workDir kinoko7danmaku.spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 打包失败，原有的 $targetExe 保持不变。"
    }
    if (-not (Test-Path -LiteralPath $builtExe -PathType Leaf)) {
        throw "打包完成后没有找到 $builtExe，原有主程序保持不变。"
    }

    $builtHash = (Get-FileHash -LiteralPath $builtExe -Algorithm SHA256).Hash
    $backupExe = Join-Path $buildRoot ("Kinoko7Danmaku-previous-$([guid]::NewGuid().ToString('N')).exe")
    try {
        if (Test-Path -LiteralPath $targetExe -PathType Leaf) {
            [System.IO.File]::Replace($builtExe, $targetExe, $backupExe)
        } else {
            [System.IO.File]::Move($builtExe, $targetExe)
        }
    } catch {
        throw "无法覆盖 $targetExe。请先退出正在运行的程序；原有主程序保持不变。$($_.Exception.Message)"
    }

    $targetHash = (Get-FileHash -LiteralPath $targetExe -Algorithm SHA256).Hash
    if ($targetHash -ne $builtHash) {
        throw "主程序校验失败，请检查 $targetExe；旧版备份在 $backupExe。"
    }
    if (Test-Path -LiteralPath $backupExe -PathType Leaf) {
        Remove-Item -LiteralPath $backupExe -Force
    }

    Write-Host "已更新 $targetExe"
    Write-Host "SHA-256: $targetHash"
    Write-Host '今后直接双击项目根目录的 Kinoko7Danmaku.exe。'
} finally {
    Pop-Location
}
