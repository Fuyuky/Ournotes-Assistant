Windows x64 / macOS Apple Silicon 免安装版，保留浏览器控制台。

- 下载对应平台 ZIP，完整解压到可写目录后运行“启动控制台”。无需安装 Python 或开发环境。
- MuMu 与游戏自行安装，在控制台配置安装路径和云端接入凭据。
- 配置、运行恢复状态及日志保存在 `user-data`；系统加密凭据不随目录跨机器迁移。
- 初装与应用内更新使用同一个完整 ZIP，校验信息内置，无需另下载更新包。
- Windows 与 Mac 分别由 GitHub Actions 原生构建，验证随包依赖、离线 OCR 和 WebUI 启动。

Mac 仅支持 Apple Silicon。MuMu 的游戏全流程尚未完成实机验收；本发行未使用 Apple Developer ID 签名或公证，首次运行可能受系统安全检查限制。
