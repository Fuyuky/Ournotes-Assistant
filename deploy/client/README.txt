OurNotes Assistant 免安装版

将 ZIP 完整解压到可写目录。
Windows：双击“启动控制台.cmd”。
macOS Apple Silicon：双击“启动控制台.command”。不支持 Intel Mac。
无需安装 Python、pip、Node.js。MuMu 与游戏需自行安装。

在控制台配置 MuMu 安装目录和云端连接；接入凭据从服务器 /admin 查询。
Mac 还需在 user-data/config/mumu.json 配置实例实际 ADB 端口。
关闭浏览器标签页不会停止后台；结束程序请使用网页内“关闭控制台”。
启动失败查看 user-data/state/webui/boot.log 和 server.log。

程序位于 versions；配置、恢复状态和日志位于 user-data。
不要通过解压新包覆盖旧目录升级，请使用控制台更新功能。
更新与初装使用同一个 ZIP；签名验证由程序自动完成。
凭据由 Windows DPAPI / macOS Keychain 保护，不随文件夹跨机器迁移。

Mac 版本提供原生运行时；MuMu 全流程仍需实机验收。
本包未使用 Apple Developer ID 签名或公证，首次运行可能受系统安全检查限制。
