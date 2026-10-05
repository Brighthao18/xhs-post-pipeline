# 可选小红书后端

覆盖层保留固定上游版本，新增本项目需要的 `/xhs-integration/health`、`identity`、`preflight`、`submit`、`management-evidence` 和 `attempts/{id}/observations` 端点。原版可执行程序不一定兼容这些协议。

## 取得源码与离线检查

需要 Git、兼容上游 `go.mod` 的 Go 工具链，以及实际接入时的原生 Chrome。准备源码不会启动服务或登录账号：

```powershell
python integrations/xiaohongshu-mcp/setup_source.py --destination Codex/work/xhs-mcp-local
New-Item -ItemType Directory -Path Codex/state/xhs-mcp-local
Set-Location Codex/work/xhs-mcp-local
go test ./... -run '^TestIntegration' -count=1
go test github.com/xpzouying/headless_browser -run '^TestNativeChrome' -count=1
go build -trimpath -o ../../state/xhs-mcp-local/xiaohongshu-mcp.exe .
```

`--upstream-source` 可指定本地上游克隆进行离线准备；已有目标目录不会被覆盖。补丁应用前校验每个文件的 SHA-256。测试只选择本项目的离线协议检查；不要把完整上游所有测试当作默认离线命令。

## 接入服务

用户自行建立新的私有状态目录和服务令牌，使用以下环境变量：

- `AUTH_TOKEN`：私有令牌，与 Python 配置 `auth_token_path` 保存的令牌一致。
- `XHS_STATE_DIR`：独立会话和证据的绝对目录，应为令牌文件旁的 `session`。
- `COOKIES_PATH`：该独立会话下的 `cookies.json`。
- `XHS_BROWSER_PATH`：实际原生 Chrome 的绝对路径。
- `XHS_LOGIN_VISIBLE=1`：专用登录过程允许显示扫码页面。

服务参数采用 `-port=127.0.0.1:18060 -headless=true`，只监听回环地址。Python 的 `publication_backend` 设置 `type: xiaohongshu_mcp`、`base_url: http://127.0.0.1:18060`、`auth_token_path`；`account` 则填写真实观察的 `nickname`、`account_id` 和 `user_id`。

服务需使用独立登录状态，不导入普通浏览器会话。实际准备、看图、声明、提交和回查的验证顺序见根目录工作流文档。此源码准备与离线测试不代表真实平台账号已完成验收。

许可与修改范围见 [MODIFICATIONS.md](MODIFICATIONS.md)、[overlay/LICENSE](overlay/LICENSE) 及嵌套浏览器模块许可证。
