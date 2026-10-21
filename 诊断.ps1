# ============================================================
#  Video-to-Doc 环境诊断脚本
#
#  用途：把"打开输出目录失败"相关的环境信息一次性收集到一个文件里，
#        便于定位是文件关联、权限、安全软件还是系统 shell 的问题。
#
#  运行方式（任选其一）：
#    1) 右键本文件 → "使用 PowerShell 运行"
#    2) Win+R 粘贴：
#       powershell -NoProfile -ExecutionPolicy Bypass -File "完整路径\诊断.ps1"
#
#  结果写入同目录下的 诊断结果.txt，不会修改任何系统设置。
# ============================================================

$ErrorActionPreference = 'Continue'
$outFile = Join-Path $PSScriptRoot '诊断结果.txt'
$lines = New-Object System.Collections.Generic.List[string]

function W([string]$text) {
    $lines.Add($text)
    Write-Host $text
}

function Try-Run([string]$label, [scriptblock]$block) {
    try {
        $v = & $block
        W ("  {0,-26} {1}" -f $label, ($v -join ' | '))
    } catch {
        W ("  {0,-26} 读取失败: {1}" -f $label, $_.Exception.Message)
    }
}

W "============================================================"
W " Video-to-Doc 环境诊断"
W (" 时间: {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
W "============================================================"

# ---------- 1. 基本信息 ----------
W ""
W "[1] 基本信息"
Try-Run "Windows 版本" {
    $o = Get-CimInstance Win32_OperatingSystem
    "$($o.Caption) $($o.Version) build $($o.BuildNumber)"
}
Try-Run "当前用户" { "$env:USERDOMAIN\$env:USERNAME" }
Try-Run "会话 ID" { (Get-Process -Id $PID).SessionId }
Try-Run "是否管理员" {
    ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}
Try-Run "PowerShell 版本" { $PSVersionTable.PSVersion.ToString() }
Try-Run "当前目录" { (Get-Location).Path }

# ---------- 2. 文件关联 ----------
W ""
W "[2] 文件关联（.bat / .cmd / .vbs / 文件夹）"
foreach ($ext in @('.bat', '.cmd', '.vbs', '.ps1')) {
    Try-Run ("HKCR$ext") {
        $v = (Get-ItemProperty ("Registry::HKEY_CLASSES_ROOT\$ext") -Name '(default)' -ErrorAction Stop).'(default)'
        if ([string]::IsNullOrWhiteSpace($v)) { '(空值 = 关联缺失)' } else { $v }
    }
}
Try-Run "HKCR\batfile" { Test-Path 'Registry::HKEY_CLASSES_ROOT\batfile' }
Try-Run "batfile\shell\open\command" {
    (Get-ItemProperty 'Registry::HKEY_CLASSES_ROOT\batfile\shell\open\command' -Name '(default)' -ErrorAction Stop).'(default)'
}
Try-Run "VBSFile\shell\open\command" {
    (Get-ItemProperty 'Registry::HKEY_CLASSES_ROOT\VBSFile\shell\open\command' -Name '(default)' -ErrorAction Stop).'(default)'
}
Try-Run "HKCR\Directory\shell 子键" {
    (Get-ChildItem 'Registry::HKEY_CLASSES_ROOT\Directory\shell' -ErrorAction Stop |
        Select-Object -ExpandProperty PSChildName) -join ','
}
Try-Run "HKCR\Folder\shell 子键" {
    (Get-ChildItem 'Registry::HKEY_CLASSES_ROOT\Folder\shell' -ErrorAction Stop |
        Select-Object -ExpandProperty PSChildName) -join ','
}
Try-Run "Directory 默认值" {
    (Get-ItemProperty 'Registry::HKEY_CLASSES_ROOT\Directory' -Name '(default)' -ErrorAction Stop).'(default)'
}

# ---------- 3. 环境变量 ----------
W ""
W "[3] 环境变量"
Try-Run "PATH" { $env:PATH }
Try-Run "SystemRoot" { $env:SystemRoot }
Try-Run "TEMP" { $env:TEMP }
Try-Run "USERPROFILE" { $env:USERPROFILE }
Try-Run "System32 是否存在" { Test-Path "$env:SystemRoot\System32\cmd.exe" }
Try-Run "explorer.exe 是否存在" { Test-Path "$env:SystemRoot\explorer.exe" }

# ---------- 4. 关键路径可访问性 ----------
W ""
W "[4] 路径可访问性"
$target = Join-Path $PSScriptRoot 'output'
$probePaths = [ordered]@{
    '输出目录'   = $target
    '项目目录'   = $PSScriptRoot
    '用户临时目录' = $env:TEMP
    'C:\Windows' = 'C:\Windows'
}
foreach ($kv in $probePaths.GetEnumerator()) {
    $p = $kv.Value
    Try-Run $kv.Key {
        if (-not (Test-Path -LiteralPath $p)) { return '路径不存在' }
        $item = Get-Item -LiteralPath $p -Force -ErrorAction Stop
        $n = @(Get-ChildItem -LiteralPath $p -Force -ErrorAction Stop).Count
        "OK 可列目录($n 项) 属性=$($item.Attributes)"
    }
}

# ---------- 5. ShellExecute 实测 ----------
W ""
W "[5] ShellExecute 打开测试（不弹窗，仅取错误码）"
if (-not ('VtdShellProbe' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class VtdShellProbe {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct SHELLEXECUTEINFO {
        public int cbSize;
        public uint fMask;
        public IntPtr hwnd;
        public string lpVerb;
        public string lpFile;
        public string lpParameters;
        public string lpDirectory;
        public int nShow;
        public IntPtr hInstApp;
        public IntPtr lpIDList;
        public string lpClass;
        public IntPtr hkeyClass;
        public uint dwHotKey;
        public IntPtr hIcon;
        public IntPtr hProcess;
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool ShellExecuteExW(ref SHELLEXECUTEINFO lpExecInfo);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr h);

    // 返回 0 = 成功；否则为 Win32 错误码
    public static int Probe(string path, string verb) {
        var sei = new SHELLEXECUTEINFO();
        sei.cbSize = Marshal.SizeOf(typeof(SHELLEXECUTEINFO));
        sei.fMask = 0x40 | 0x400 | 0x0C;   // NOCLOSEPROCESS | NO_UI | INVOKEIDLIST
        sei.lpVerb = verb;
        sei.lpFile = path;
        sei.nShow = 1;
        bool ok = ShellExecuteExW(ref sei);
        int err = ok ? 0 : Marshal.GetLastWin32Error();
        if (sei.hProcess != IntPtr.Zero) { CloseHandle(sei.hProcess); }
        return err;
    }
}
'@
}
foreach ($kv in $probePaths.GetEnumerator()) {
    foreach ($verb in @($null, 'open', 'explore')) {
        $label = "{0} [{1}]" -f $kv.Key, ($(if ($null -eq $verb) { '默认' } else { $verb }))
        Try-Run $label {
            $code = [VtdShellProbe]::Probe($kv.Value, $verb)
            if ($code -eq 0) { '成功' } else { "失败 错误码=$code" }
        }
    }
}

# ---------- 6. explorer.exe 启动测试 ----------
W ""
W "[6] explorer.exe 启动测试"
Try-Run "启动 explorer 打开输出目录" {
    $p = Start-Process -FilePath "$env:SystemRoot\explorer.exe" -ArgumentList "`"$target`"" -PassThru -ErrorAction Stop
    Start-Sleep -Milliseconds 1500
    if ($p.HasExited) { "进程已退出 退出码=$($p.ExitCode)" } else { '进程仍在运行（可能已打开窗口）'; $p | Stop-Process -ErrorAction SilentlyContinue }
}
Try-Run "cmd /c start 测试" {
    $p = Start-Process -FilePath "$env:SystemRoot\System32\cmd.exe" -ArgumentList "/c start `"`" `"$target`"" -PassThru -WindowStyle Hidden -ErrorAction Stop
    Start-Sleep -Milliseconds 1500
    if ($p.HasExited) { "退出码=$($p.ExitCode)" } else { '仍在运行'; $p | Stop-Process -ErrorAction SilentlyContinue }
}

# ---------- 7. 安全软件 ----------
W ""
W "[7] 安全软件"
Try-Run "已注册杀毒软件" {
    Get-CimInstance -Namespace 'root/SecurityCenter2' -ClassName AntiVirusProduct -ErrorAction Stop |
        ForEach-Object { "$($_.displayName)(状态=$($_.productState))" }
}
Try-Run "Defender 受控文件夹访问" {
    $s = Get-MpPreference -ErrorAction Stop
    "EnableControlledFolderAccess=$($s.EnableControlledFolderAccess)"
}
Try-Run "Defender 实时保护" {
    (Get-MpComputerStatus -ErrorAction Stop).RealTimeProtectionEnabled
}

# ---------- 8. 组策略限制 ----------
W ""
W "[8] 相关组策略键"
foreach ($k in @(
    'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Explorer',
    'HKCU:\Software\Policies\Microsoft\Windows\Explorer',
    'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Safer\CodeIdentifiers',
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Explorer'
)) {
    Try-Run $k { if (Test-Path $k) { '存在: ' + ((Get-ItemProperty $k).PSObject.Properties | Where-Object { $_.Name -notlike 'PS*' } | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join '; ' } else { '不存在' } }
}

# ---------- 9. 最近的应用错误事件 ----------
W ""
W "[9] 最近 20 条应用程序错误事件（Application 日志）"
Try-Run "错误事件" {
    Get-WinEvent -FilterHashtable @{LogName='Application'; Level=2; StartTime=(Get-Date).AddDays(-2)} -MaxEvents 20 -ErrorAction Stop |
        ForEach-Object { "{0:MM-dd HH:mm} {1} {2}" -f $_.TimeCreated, $_.ProviderName, ($_.Message -split "`n")[0] }
}

# ---------- 输出 ----------
W ""
W "============================================================"
W " 诊断结束"
W "============================================================"
try {
    $lines -join "`r`n" | Set-Content -LiteralPath $outFile -Encoding UTF8
    Write-Host ""
    Write-Host "结果已保存到: $outFile" -ForegroundColor Green
} catch {
    Write-Host "保存结果失败: $($_.Exception.Message)" -ForegroundColor Red
}
Write-Host ""
if (-not $env:VTD_NOPAUSE) { Read-Host "按回车键关闭" }
