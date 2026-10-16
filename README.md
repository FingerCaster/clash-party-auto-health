# Clash Party 自动健康隔离

本项目是配合 [Clash Party](https://github.com/mihomo-party-org/clash-party) 使用的第三方本地辅助扩展，不是 Clash Party 内置插件，也不是独立代理客户端。它通过本地覆写和系统后台任务增强自动选择的健康管理能力，需要先安装 Clash Party 并导入订阅。

在每台电脑本地检测 Clash Party 的自动选择节点：持续超时或失败的节点会暂时从 `url-test` 自动组排除，恢复后自动加回。节点仍保留在原始节点列表和手动选择组中。

程序读取 Mihomo 的健康检查记录，生成 Clash Party 本地覆写，并通过后台任务定时维护隔离名单。不依赖 Sub-Store 或共享服务器；每台电脑独立保存自己的订阅绑定和健康状态。

## Clash Party 项目与下载

- 官方项目：[mihomo-party-org/clash-party](https://github.com/mihomo-party-org/clash-party)
- 安装包下载：[GitHub Releases](https://github.com/mihomo-party-org/clash-party/releases)
- 官网与使用文档：[clashparty.org](https://clashparty.org/)

## 安装前准备

1. 安装并启动 Clash Party，导入订阅并选中需要绑定的订阅。
2. 生成的配置须包含内联 `proxies` 和至少一个 `url-test` 自动组；只使用 `proxy-providers` 的配置暂不支持。
3. 安装 Python 3 和 Node.js，然后完全退出 Clash Party。

## macOS 安装

需要 macOS、Python 3、Node.js 和 Clash Party。默认应用路径为 `/Applications/Clash Party.app`，默认数据目录为 `~/Library/Application Support/mihomo-party`。

下载本仓库 ZIP 并解压，或运行：

```sh
git clone https://github.com/FingerCaster/clash-party-auto-health.git
cd clash-party-auto-health
```

退出 Clash Party，然后执行：

```sh
./install_macos.sh
```

非标准目录可显式传入 Clash Party `Contents/Resources` 和应用数据目录：

```sh
./install_macos.sh \
  --resources-dir "/Applications/Clash Party.app/Contents/Resources" \
  --data-dir "$HOME/Library/Application Support/mihomo-party"
```

安装完成后启动 Clash Party，后台每分钟检查一次。查看状态和日志：

```sh
cat "$HOME/Library/Application Support/mihomo-party/adaptive-auto-selection/status.json"
tail -n 30 "$HOME/Library/Application Support/mihomo-party/adaptive-auto-selection/agent.log"
```

卸载后台任务：

```sh
./uninstall_macos.sh
```

然后在 Clash Party 当前订阅的覆写设置里解除绑定“自动选择健康隔离与恢复”，并重新加载订阅。卸载脚本保留覆写文件和健康历史，避免在解除订阅绑定前破坏现有配置。

## Windows 10 / 11 安装

需要当前 Windows 用户下安装 Clash Party、带 `pythonw.exe` 的 Python（推荐 3.13）和 Node.js LTS。解压仓库 ZIP，在 PowerShell 中进入该目录，先运行只读预检查：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_windows.ps1
```

预检查通过后，从系统托盘完全退出 Clash Party，再执行安装：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_windows.ps1 -Install
```

便携版或自定义安装路径可传 `-AppDir`、`-DataDir`、`-PythonPath` 和 `-NodePath`。详细步骤、状态检查和卸载方式见 [Windows 安装说明](WINDOWS.md)。

## 检测规则

- 探测 `https://www.gstatic.com/generate_204`，要求在超时内收到 HTTP 204。
- 连续失败 3 次，或至少有 8 个有效样本且最近最多 10 次中至少失败 3 次，就隔离节点。
- 隔离节点约每 2–3 分钟复测一次，连续通过 3 次后恢复到符合既有筛选条件的自动组。
- 只修改 `url-test` 自动组；原节点和手动组不变。自动组没有候选时使用 `REJECT`。
- 可修改 `adaptive-auto-selection/policy.json` 调整规则参数，下一轮检测时生效。

探测成功只说明指定探测地址返回了 HTTP 204，不代表所有网站都可访问。切换到其他订阅时，检测会暂停；切回安装时绑定的订阅后继续运行。

## 数据与隐私

状态、日志和安装备份保存在 Clash Party 本机数据目录。备份可能包含订阅凭据，请勿分享或提交。本仓库不包含任何用户订阅、节点或检测状态。

## 开发与测试

```sh
python3 -m unittest discover -s . -p 'test_*.py' -v
```

Windows 任务注册和命名管道通信应在真实 Windows 电脑完成最终验证。

## 许可证

本项目使用 GNU Affero 通用公共许可证第 3 版，见 [LICENSE](LICENSE)。
