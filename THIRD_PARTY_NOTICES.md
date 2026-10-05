# 第三方代码与资源

根目录 MIT 许可证适用于本项目自有代码、文档和示例。以下上游代码、依赖和资源保留原有权利与许可，不被重新授权为本项目 MIT。

| 组件 | 来源与版本 | 许可 / 分发方式 |
| --- | --- | --- |
| 小红书后端补丁 | [xpzouying/xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp)，提交 `a5c8f7799980ba1fdd501999843eb2d17e4c9a9f` | Apache-2.0；仅分发必要修改文件和原 `LICENSE`，来源清单见 `integrations/xiaohongshu-mcp/upstream.json` |
| 嵌套浏览器模块 | [xpzouying/headless_browser](https://github.com/xpzouying/headless_browser)，v0.4.0 本地兼容修改 | 原 MIT，保留 Copyright (c) 2025 zy 及完整许可证；修改说明见嵌套 `PATCH.md` |
| WeRSS | [rachelos/we-mp-rss](https://github.com/rachelos/we-mp-rss)，容器镜像固定 digest 见接入文档 | 上游 MIT；本项目只分发自己的引导 / 适配脚本，附上游许可便于查阅，不分发镜像或账号状态 |
| Pillow | [python-pillow/Pillow](https://github.com/python-pillow/Pillow)，Python 依赖 | 上游许可随依赖安装；未复制其源码或二进制到本源码包 |
| Noto CJK 测试字体 | [notofonts/noto-cjk](https://github.com/notofonts/noto-cjk) 固定提交 | SIL Open Font License；CI 从上游下载到临时目录并保存许可证，不随本项目源码发行 |
| 宋体 / Times New Roman | 使用者的系统安装 | 不分发字体，不宣称字体获得本项目 MIT 授权 |

后端衍生文件的显著修改标记、修改概述及完整 Apache-2.0 文本保留在对应目录；新增或再次修改上游文件时同步更新 `upstream.json` 的哈希和说明。第三方 Go / Python 依赖由正常依赖机制取得，不把全局缓存纳入发行。
