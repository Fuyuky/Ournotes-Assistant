# OurNotes Assistant：客户端安装与运行指南

此包为同一业务客户端的 Windows x64 或 Apple Silicon macOS 发行包。不支持 Intel Mac。解压即用，不需要系统 Python、pip、uv、Git 或管理员权限；服务器与开发维护工具另行部署。

## 安装与检查

1. 将 `ournotes-client-<版本>-<平台>.zip` 完整解压到用户可写目录，不覆盖已有 `user-data`。已有安装通过控制台更新。
2. 读取 `current.json`；`version` 必须符合 `^\d+\.\d+\.\d+$`，`platform` 必须与本机一致。核对 `update-public-key.txt`、`versions/<版本>/app/scripts/webui.py` 和当前平台的运行时。
3. Windows 使用 `versions/<版本>/runtime/python.exe` / `pythonw.exe`；Mac 使用 `versions/<版本>/runtime/bin/python3`。所有 Python 操作都使用随包运行时，不创建 venv、不重装依赖。
4. 核对 `user-data/data/catalog/current.json` 及其实际快照；运行素材缺失应报告不完整包。不要用空目录代替素材。
5. 内置运行时检查导入 `cv2, numpy, cryptography, onnxruntime, fastapi, uvicorn, websockets`，Windows 再导入 `win32crypt`。

## 自动启动与验证

用户要求安装并运行时，Windows 执行根目录 `boot.ps1` 或双击 `启动控制台.cmd`；Mac 执行根目录 `boot.command` 或双击 `启动控制台.command`。启动前检查本机8766端口，已有本安装的控制台则复用，不结束其他安装或未知进程。

启动后轮询 `http://127.0.0.1:8766/api/launcher/ping`，每秒一次、最多60秒；JSON的 `app` 必须为 `ournotes-launcher`，并验证首页可读、监听进程属于本安装。超时查 `user-data/state/webui/server.log`（Mac启动错误另查 `boot.log`）；启动命令返回不代表服务已就绪。向用户提供安装路径、控制台地址和实际验证结果。

## 接入与业务权限

控制台启动不启动模拟器。业务需要已安装游戏、1600×900画面、云端HTTPS地址及独立节点凭据。Windows凭据为当前用户DPAPI，Mac为当前用户登录钥匙串；不复制其他机器凭据、不打印令牌或验证码。通用初装和更新包没有 `client-connection.json`，控制台接入页配置本机节点。

Mac使用MuMu官方 `mumutool` 管理实例、官方ADB截图与输入；工具位置和每个实例的实际ADB端口必须明确配置，不按实例号猜端口。未验证的MuMu状态格式、未知页面与未确认输入不得猜测成功。

安装与启动仅授权这些动作。游戏启动、发码、抽卡、批次恢复、账号换绑、usage状态修改和公网发布仍需本次业务授权；沿用持久账号、编号、计划、交易和核查保护。

## 更新与数据保护

- 更新匿名HTTPS读取公开仓库 `Fuyuky/Ournotes-Assistant`，不需要GitHub凭据。启动只检查，明确点击才下载；平台签名清单、ZIP大小、SHA-256与逐文件摘要都通过后才可安装。
- 更新器等待client、runner、email-rebind与update锁交接，分平台核对运行时后原子切换current指针。失败保留旧版本与数据，禁止清锁绕过运行保护。
- `versions`是程序，`user-data`是用户配置、运行素材、成果、数据库及证据。不删库、不回退编号、不造缺失档案、不重发已提交但无结果的交易，不通过重启清除核查保护。
- 只读查询数据库使用SQLite `mode=ro`；不要初始化RuntimeStore。批次完成须核对数据库归属、实际进程与锁，不能只看调度complete。
- 验证新包使用隔离空数据根，不操作其他安装的活动数据。服务器后台、Google OAuth、原始账号JSON和私人连接均不得进入公开发行包。
