# Windows 安装与卸载

Windows 安装器会将健康检测程序放入 Clash Party 数据目录，在当前订阅绑定专用覆写，生成本地策略和状态，并为当前用户注册定时任务。

## 运行要求

- Windows 10 或 Windows 11。
- 当前 Windows 用户已安装 Clash Party。
- Python 3（安装目录需含 `pythonw.exe`，推荐 Python 3.13）和 Node.js LTS。
- 当前配置有 `profile.yaml`、`work/config.yaml`、内联 `proxies` 和至少一个 `url-test` 自动组。仅使用 `proxy-providers` 的配置暂不支持。

如需安装依赖，使用 winget：

```powershell
winget install --exact --id Python.Python.3.13
winget install --exact --id OpenJS.NodeJS.LTS
```

安装完成后关闭并重新打开 PowerShell，然后检查 `py -3 --version` 和 `node --version`。Clash Party 与后台任务必须使用同一个 Windows 账户；不要让 Clash Party 以管理员身份运行而安装普通用户任务。

## 预检查与安装

在下载并解压的仓库目录中打开 PowerShell。先运行预检查：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_windows.ps1
```

预检查只读取配置并报告应用目录、数据目录、内核和当前订阅，不安装或修改功能文件。解决预检查错误后，在 Clash Party 系统托盘菜单中选择“退出”，再运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_windows.ps1 -Install
```

`ExecutionPolicy Bypass` 只对这次 PowerShell 进程生效，不会修改系统策略。安装器会先用 Mihomo 校验生成配置、备份旧文件和同名任务，再写入配置并注册任务；失败时会尝试回滚。

便携版或非标准安装路径示例：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_windows.ps1 -Install `
  -AppDir 'D:\Apps\Clash Party' `
  -DataDir 'D:\Apps\Clash Party\data' `
  -PythonPath 'C:\Python313\python.exe' `
  -NodePath 'C:\Program Files\nodejs\node.exe'
```

`-AppDir` 是包含 `resources` 的应用目录；`-DataDir` 是包含 `profile.yaml` 的应用数据目录。需要指定内核时，可使用 `-Core mihomo`、`-Core mihomo-alpha` 或 `-Core mihomo-smart`。

安装后启动 Clash Party，等待约一分钟，查看状态：

```powershell
$data = Join-Path $env:APPDATA 'mihomo-party'
$health = Join-Path $data 'adaptive-auto-selection'
Get-Content (Join-Path $health 'status.json') -Encoding UTF8
Get-Content (Join-Path $health 'agent.log') -Encoding UTF8 -Tail 30
$installation = Get-Content (Join-Path $health 'windows-installation.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Get-ScheduledTask -TaskName $installation.task_name | Get-ScheduledTaskInfo
```

便携版请把 `$data` 设置为安装时使用的 `-DataDir`。

- `updated_at` 应随每轮任务更新；`rules_applied: true` 表示规则已应用。
- `quarantined_count` 是当前隔离节点数，为 0 也可能正常。
- `quarantine` 和 `recover` 日志事件分别表示隔离和恢复。
- `inactive_profile` 表示当前选中的不是安装时绑定的订阅。
- `core_not_running` 表示未找到对应配置的 Mihomo 内核，请检查应用是否启动、目录和账户是否一致。
- `cycle_error` 表示检测时发生错误，后面会记录错误类型和信息。

需要立即运行一轮时：

```powershell
py -3 (Join-Path $health 'auto_health.py') --data-dir $data
```

## 暂停与卸载

暂停任务：

```powershell
Disable-ScheduledTask -TaskName $installation.task_name
```

恢复任务：

```powershell
Enable-ScheduledTask -TaskName $installation.task_name
Start-ScheduledTask -TaskName $installation.task_name
```

彻底卸载时，删除定时任务，在 Clash Party 当前订阅的覆写设置中解除绑定“自动选择健康隔离与恢复”，然后重新加载订阅：

```powershell
Unregister-ScheduledTask -TaskName $installation.task_name -Confirm:$false
```

备份位于 `adaptive-auto-selection\backups`。备份可能包含订阅凭据，请留在本机。
