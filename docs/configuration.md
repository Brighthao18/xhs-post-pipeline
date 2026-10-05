# 配置与字体

`xhs-pipeline init --workspace .` 创建私有配置，默认 `sources: []`、`allow_publish: false`、`submit_backend_ready: false`。不会继承安装机器的账号、订阅、登录状态或发布授权。可通过 `--output` 指定新配置文件；已有文件不会被覆盖。

## 路径

| 字段 | 含义 |
| --- | --- |
| `workspace` | 已存在的工作区；相对配置文件所在目录解析 |
| `state_dir` | SQLite 账本目录；默认 `Codex/state/xhs-post` |
| `work_dir` | 来源、草稿、图像意图和审阅证据；默认 `Codex/work/xhs-post` |
| `output_dir` | 生成素材目录；默认 `output` |
| `skill_dir` | 可选生成器目录，含 `scripts/generate.py`；省略时使用自带实现 |
| `author` | 素材署名；草稿省略署名时使用该字段，显式不一致会被拒绝 |

其他相对路径，包括来源夹具 `path` / `html_path`、`refresh.credentials_path` 与 `publication_backend.auth_token_path`，均以工作区为基准。已有绝对路径配置继续有效。只有自行信任的代码才应配置为 `skill_dir`，因为生成器是实际执行的 Python 代码。

当前账本日界线和发布时段采用 `Asia/Singapore`，即 UTC+08；其他时区尚未支持。

## 字体

Windows 优先使用系统宋体和 Times New Roman。macOS 尝试 Songti / Times New Roman；Linux 尝试 Noto Serif CJK / Times New Roman，找不到后者时使用 Liberation Serif。Linux 的替代字体不等同于宋体或 Times New Roman；`--doctor` 返回实际文件和字体名称，便于确认。

优先级是素材或配置中的 `fonts` → 环境变量 → 系统候选。配置示例：

```json
"fonts": {"cjk": "/absolute/path/to/cjk-font.ttc", "latin": "/absolute/path/to/latin-font.ttf"}
```

也可以设置 `XHS_CJK_FONT` 和 `XHS_LATIN_FONT`。例如 Linux：

```sh
export XHS_CJK_FONT=/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc
export XHS_LATIN_FONT=/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf
```

缺字或找不到字体会报错；不会静默丢字。项目和发行包都不附带商用字体。安装系统字体后重新检查，适用许可由字体提供方决定。

## 来源与账号

RSS 示例字段如下，需要替换为有权访问的真实来源，并在私有配置中启用：

```json
{"id": "my-feed", "type": "rss", "enabled": true, "url": "https://example.com/feed.xml", "new_only": true, "domain": "general"}
```

`new_only: true` 首次成功列表获取建立历史基线，只处理之后新增的文章。订阅摘要不自动视为已读全文，正文缺失或源服务失败时不会伪造完成状态。

发布账号必须使用实际观察到的稳定 `account_id` / `user_id` 绑定，昵称不足以确认身份。发布默认禁用；接入服务并核对身份、编辑器、图片顺序和声明控件后，才能按自身授权显式启用策略。凭据文件只放在私有状态目录中，不填写到示例或提交记录。
