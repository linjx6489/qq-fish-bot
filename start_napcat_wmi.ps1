# start_napcat_wmi.ps1 -- start the standalone NapCat detached from any console.
#
# Why WMI: when NapCat is started from a console/PowerShell job, Windows tears the
# process tree down as soon as that console goes away (seen 2026-10-23: NapCat died
# when the launching job window closed). A process created through
# Win32_Process.Create gets no console of its own and survives.
#
# Keep this file pure ASCII.

$ErrorActionPreference = 'SilentlyContinue'

# ★改成你自己的 NapCat 独立版目录（node.exe + index.js 那一层）
$napDir = 'C:\NapCat\shell_node'
$nodeExe = Join-Path $napDir 'node.exe'
$indexJs = Join-Path $napDir 'index.js'
$logFile = 'C:\qq-fish-bot\data\napcat.log'
$uin = '123456789'

# 1) kill only the stale standalone NapCat (never taskkill /im node.exe:
#    this machine also runs other node processes, e.g. the billion-context proxy)
Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
    Where-Object { $_.CommandLine -like '*shell_node*' } |
    ForEach-Object {
        Write-Host ("  kill stale NapCat pid " + $_.ProcessId)
        Stop-Process -Id $_.ProcessId -Force
    }
Start-Sleep -Seconds 2

# 2) create it detached, output appended to data\napcat.log
$cmd = 'cmd.exe /c cd /d "' + $napDir + '" && "' + $nodeExe + '" "' + $indexJs + '" -q ' + $uin + ' >> "' + $logFile + '" 2>&1'
$res = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
if ($res.ReturnValue -ne 0) {
    Write-Host ("  WMI create failed, ReturnValue=" + $res.ReturnValue)
    exit 1
}
Write-Host ("  NapCat spawned, pid " + $res.ProcessId)

# 3) wait for the OneBot websocket port
for ($i = 1; $i -le 30; $i++) {
    Start-Sleep -Seconds 3
    $listen = (netstat -ano | Select-String ':3001' | Select-String 'LISTENING' | Measure-Object).Count
    if ($listen -ge 1) {
        Write-Host ("  port 3001 ok after " + ($i * 3) + "s")
        exit 0
    }
}
Write-Host '  port 3001 never came up (NapCat may be waiting for a QR login - open http://127.0.0.1:6099/webui/)'
exit 2
