# 源码发行

公开范围以 `public-files.txt` 为准，清单只列源代码、无账号示例、许可、文档和检查配置。`Codex/`、旧脚本、私人配置、素材与历史运行记录不随发行复制。

## 发行前检查

1. 更新版本号、变更记录和公开清单，运行两组本地测试。
2. 执行 `python scripts/audit_public.py --check-git`，核对暂存范围。
3. 使用 `python -m build` 构建 Python 源码包和安装包，再从隔离目录安装并生成示例。
4. 使用下面的命令导出源码 ZIP，验证包含的哈希清单和 CRC。

```powershell
python scripts/export_public.py --output Codex/outputs/xhs-post-pipeline-source.zip
```

已有发行包不会被覆盖。ZIP 包含 `RELEASE_MANIFEST.json`，记录每个源文件的 SHA-256 与字节数。Python 安装包只包含 Python 核心与自带生成器；可选服务源码保留在完整源码 ZIP 中，各自维护原许可证。

将干净源码树推送到自己选择的仓库之前，应再次人工核对公开文件、许可证和联系方式。项目提供 CI 配置，但只有实际远程运行才有 CI 通过记录；源码 ZIP 生成成功不表示已上传 GitHub 或 PyPI。
