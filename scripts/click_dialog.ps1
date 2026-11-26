param(
    [string]$Class = "#32770",
    [string]$ButtonMatch = "(N)",
    [int]$PidFilter = 0
)
$ErrorActionPreference = "Continue"
$sig = @'
using System;
using System.Runtime.InteropServices;
public delegate bool EnumProcD(IntPtr hWnd, IntPtr lParam);
public static class WinD {
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProcD proc, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr parent, EnumProcD proc, IntPtr lParam);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, System.Text.StringBuilder sb, int max);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr hWnd, System.Text.StringBuilder sb, int max);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);
}
'@
Add-Type -TypeDefinition $sig
$dialogs = New-Object System.Collections.ArrayList
$cbTop = {
    param($hWnd, $lParam)
    $cls = New-Object System.Text.StringBuilder 256
    [WinD]::GetClassName($hWnd, $cls, 256) | Out-Null
    if ($cls.ToString() -eq $script:Class) {
        $pid2 = [uint32]0
        [WinD]::GetWindowThreadProcessId($hWnd, [ref]$pid2) | Out-Null
        if ($script:PidFilter -eq 0 -or $script:PidFilter -eq [int]$pid2) {
            [void]$script:dialogs.Add(@{ H = $hWnd; P = $pid2 })
        }
    }
    return $true
}
[WinD]::EnumWindows($cbTop, [IntPtr]::Zero) | Out-Null
Write-Output ("DIALOGS=" + $dialogs.Count)
foreach ($d in $dialogs) {
    $h = $d.H
    $title = New-Object System.Text.StringBuilder 512
    [WinD]::GetWindowText($h, $title, 512) | Out-Null
    Write-Output ("DIALOG pid=" + $d.P + " hwnd=" + $h + " title=" + $title.ToString())
    $script:clicked = $false
    $script:match = $ButtonMatch
    $cbChild = {
        param($c, $l)
        $cls = New-Object System.Text.StringBuilder 128
        [WinD]::GetClassName($c, $cls, 128) | Out-Null
        $txt = New-Object System.Text.StringBuilder 256
        [WinD]::GetWindowText($c, $txt, 256) | Out-Null
        $t = $txt.ToString()
        if ($cls.ToString() -eq "Button") {
            Write-Output ("  BUTTON [" + $t + "] hwnd=" + $c)
            if (-not $script:clicked -and $t.Contains($script:match)) {
                $script:clicked = $true
                [void][WinD]::PostMessage($c, 0x00F5, [IntPtr]::Zero, [IntPtr]::Zero)
                Write-Output ("  CLICKED " + $t)
            }
        }
        return $true
    }
    [WinD]::EnumChildWindows($h, $cbChild, [IntPtr]::Zero) | Out-Null
    if (-not $script:clicked) {
        [void][WinD]::PostMessage($h, 0x0111, [IntPtr]7, [IntPtr]::Zero)
        Write-Output "  FALLBACK WM_COMMAND IDNO"
    }
}
