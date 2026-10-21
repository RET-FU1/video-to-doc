# ============================================================
#  Video-to-Doc 完整性级别修复脚本
#
#  症状：点「打开输出目录」报
#          "Windows 无法访问指定设备、路径或文件。你可能没有适当的权限访问该项目。"
#        程序日志里显示：完整性级别=Low / Untrusted
#
#  原因：venv\Scripts\python.exe 被设置了「低完整性标签」。
#        Windows 会据此把从它启动的进程整体降权，而低完整性进程会被
#        拒绝一切 shell 操作（打开文件夹、启动外部程序全部失败）。
#
#  本脚本把项目目录与 venv\Scripts 下所有启动器的完整性级别改回 Medium，
#  不修改任何文件内容、不改变所有者、不涉及权限（ACL）。
#
#  运行方式：右键本文件 → "使用 PowerShell 运行"
#           或 Win+R 粘贴：
#           powershell -NoProfile -ExecutionPolicy Bypass -File "完整路径\修复完整性级别.ps1"
# ============================================================

$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot

Write-Host "============================================================"
Write-Host "  Video-to-Doc 完整性级别修复"
Write-Host "============================================================"
Write-Host "项目目录: $root"
Write-Host ""

function Test-PythonLevel([string]$exe) {
    if (-not (Test-Path $exe)) { return "(不存在)" }
    $tmp = Join-Path $env:TEMP ("vtd_il_" + [guid]::NewGuid().ToString("N") + ".txt")
    try {
        & $exe -c "import subprocess;open(r'$tmp','w',encoding='utf-8').write(subprocess.run(['whoami','/groups'],capture_output=True,text=True,encoding='mbcs',errors='replace').stdout)" 2>$null | Out-Null
        if (Test-Path $tmp) {
            $m = Select-String -Path $tmp -Pattern 'S-1-16-(\d+)'
            if ($m) {
                switch ($m.Matches[0].Groups[1].Value) {
                    '8192' { return 'Medium' }
                    '4096' { return 'Low' }
                    '12288' { return 'High' }
                    default { return 'S-1-16-' + $m.Matches[0].Groups[1].Value }
                }
            }
        }
        return '(读取失败)'
    } catch {
        return '(读取失败)'
    } finally {
        Remove-Item $tmp -ErrorAction SilentlyContinue
    }
}

$py = Join-Path $root 'venv\Scripts\python.exe'
$pyw = Join-Path $root 'venv\Scripts\pythonw.exe'

Write-Host "[1] 修复前"
Write-Host ("    python.exe  : " + (Test-PythonLevel $py))
Write-Host ("    pythonw.exe : " + (Test-PythonLevel $pyw))
Write-Host ""

Write-Host "[2] 设置完整性级别为 Medium ..."
& icacls $root /setintegritylevel "(OI)(CI)M"
& icacls (Join-Path $root 'venv\Scripts') /setintegritylevel "(OI)(CI)M" /T /C /Q
Write-Host ""

Write-Host "[3] 修复后"
$lvlPy = Test-PythonLevel $py
$lvlPyw = Test-PythonLevel $pyw
Write-Host ("    python.exe  : " + $lvlPy)
Write-Host ("    pythonw.exe : " + $lvlPyw)
Write-Host ""

if ($lvlPy -eq 'Medium' -and $lvlPyw -eq 'Medium') {
    Write-Host "修复成功。请关闭并重新启动 Video-to-Doc 后再试「打开输出目录」。" -ForegroundColor Green
} else {
    Write-Host "修复未完全生效，请把上面的输出发给开发者。" -ForegroundColor Yellow
    Write-Host "若提示权限不足，请右键本文件选择「以管理员身份运行」。"
}

Write-Host ""
if (-not $env:VTD_NOPAUSE) { Read-Host "按回车键关闭" }
