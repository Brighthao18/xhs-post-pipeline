# XHS Post Pipeline

把文章整理成可审阅的小红书图文素材，并用本地账本记录来源、改写版本、图片与发布状态。

项目提供离线素材生成器和持久化工作流。逐篇创作、事实核查、图片审阅与实际平台回查由使用者或具备相应工具的代理完成；普通脚本不会自动调用模型。新配置默认关闭来源采集和发布，不携带任何个人账号、登录状态或历史内容。

## 能做什么

- 从 UTF-8 JSON 生成正文、卡片、联系表、来源审阅记录与文件哈希。
- 生成 1080 × 1440 的四卡素材，支持八种样式；也可登记并打包原生 Image 生成的一封面四卡。
- 保存来源事件、任务版本、运行租约、逐图检查和发布意图，支持中断后继续。
- 接入 RSS、Atom、JSON Feed；可选接入本机 WeRSS 与小红书发布后端。
- 把编辑器准备、提交尝试、审核中与已发布分开记录；结果不明时禁止自动重复提交。

## 快速开始：离线生成

需要 Python 3.11 或更高版本。Windows 默认使用系统宋体和 Times New Roman；其他系统的字体配置见[配置说明](docs/configuration.md)。项目不分发字体文件。

在项目根目录打开 PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install .
.venv\Scripts\xhs-materials.exe --doctor
.venv\Scripts\xhs-materials.exe --input examples/content.json --output-dir output/demo
```

macOS / Linux：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/xhs-materials --doctor
.venv/bin/xhs-materials --input examples/content.json --output-dir output/demo
```

`output/demo` 必须是新目录。生成器拒绝覆盖已有素材。输出包含 `post.txt`、`content.json`、`card_0.jpg` 至 `card_3.jpg`、`contact_sheet.jpg`、`source_review.md` 与 `_meta.json`。示例文本为自有演示内容；生成成功不代表实际发布。

## 建立本地工作流

```powershell
.venv\Scripts\xhs-pipeline.exe init --workspace . --author "你的署名"
.venv\Scripts\xhs-pipeline.exe --config config/xhs-automation.local.json doctor
.venv\Scripts\xhs-pipeline.exe --config config/xhs-automation.local.json status
```

也可以复制 `config/xhs-automation.example.json` 为 `config/xhs-automation.local.json`。相对 `workspace` 以配置文件所在目录为基准，其余相对路径以工作区为基准。默认使用项目自带生成器，安装到新电脑时无需复制个人 Codex 目录。

`init` 拒绝覆盖已有配置。实际账号、来源订阅、字体、发布策略及私有凭据路径由使用者填写。`doctor` 只做本地检查，不发起平台提交；没有绑定账号和发布后端时，列出的发布阻塞是预期结果。

`xhs-pipeline` 与 `xhs-materials` 都输出 UTF-8 JSON，重定向到文件或管道时也一样。PowerShell 按 `[Console]::OutputEncoding` 解码原生命令输出；需要把结果保存到变量时，先执行 `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8`。

[工作流说明](docs/workflow.md)介绍租约、草稿、逐图任务与发布回查；[接入说明](docs/integrations.md)介绍可选服务和能力限制。Python 命令只推进已接入的阶段，不会自行完成代理创作、调用 Image 或创建 Codex 定时任务。

## 项目结构

| 目录 | 用途 |
| --- | --- |
| `pipeline/v2/` | 任务状态、来源、图像任务、质量检查、发布桥接与命令行 |
| `pipeline/assets/scripts/` | 自带离线生成器与图片资产协议 |
| `examples/`、`config/` | 无账号数据的示例与默认关闭发布的配置 |
| `integrations/` | 可选 WeRSS 接入和固定上游版本的小红书后端补丁 |
| `scripts/`、`public-files.txt` | 公开文件审计与可复核源码导出 |
| `docs/` | 配置、贡献、接入与发行说明 |

运行配置、Cookie、数据库、来源证据和生成素材均保存在被忽略的本地目录。原工作区的旧版脚本、自动化记忆和品牌素材保留在本机，未纳入公开发行。公开源码 ZIP、Python 源码包与安装包都采用明确的文件范围，不应直接压缩整个私人工作区。

## 开发和检查

```powershell
python -m pip install ".[dev]"
python -m unittest discover -s pipeline/v2/tests
python -m unittest discover -s tests
python scripts/audit_public.py
python -m build
```

测试使用临时目录、离线夹具及本机模拟 HTTP 服务，不连接真实平台，也不读取个人账号。GitHub Actions 提供 Windows / Linux、Python 3.11 / 3.12 的自动检查配置；实际运行结果以 CI 记录为准。本地生成、模拟审阅与传输测试都不能替代真实来源或实际发布验收。

贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全问题处理见 [SECURITY.md](SECURITY.md)，源码导出和上线前核查见[发行说明](docs/releasing.md)。

## 许可证

本项目自有 Python 代码、文档及示例采用 [MIT](LICENSE)，使用标准 [MIT 许可证文本](https://opensource.org/license/mit)。`integrations/xiaohongshu-mcp/overlay/` 中的上游及衍生代码保留 Apache-2.0；其嵌套浏览器模块保留原 MIT 许可，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。平台文章、品牌、图片、账号与系统字体不属于本项目的授权范围。
