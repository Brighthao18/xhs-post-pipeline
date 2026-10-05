# 工作流与能力边界

素材生成器可单独离线运行。持久化工作流在此基础上保存每一步的输入、状态和证据；创作、内容判断和平台页面观察由实际执行者提供。

## 运行租约

先执行 `begin-run --owner <本轮唯一标识>`，读取 `lease_token`。可变命令使用全局参数 `--lease-token <令牌>`，放在子命令之前。结束时执行 `end-run`；活跃运行不重入，过期令牌不能继续进行外部动作。

```powershell
xhs-pipeline --config config/xhs-automation.local.json begin-run --owner local-review
xhs-pipeline --config config/xhs-automation.local.json --lease-token <令牌> run-once
xhs-pipeline --config config/xhs-automation.local.json --lease-token <令牌> end-run
```

`run-once` 执行已配置的来源发现和正文获取，再列出后续阶段。`next` 返回待办，`status` 返回账本；命令不会自行生成文案、调用模型或发布。

## 内容和图片

完整来源获取后，通过 `draft --job-id <id> --input <草稿JSON>` 登记独立创作的内容。`review` 必须提供与当前 `job_id`、`source_hash`、`content_hash` 一致的逐项核查。健康领域还需要实际核对的证据 URL；勾选字段不是事实核查本身。

普通四卡内容可在语义核查通过后 `render`。五图内容使用 `visual.mode: imagegen_native`：

1. `plan-images` 登记封面和四卡；`image-intent` 在实际工具调用前保存意图。
2. 通过真实 Image 工具生成后，`image-result` 登记工具返回的本地原图与回执。
3. 实际查看原图和手机尺度缩略图后，`image-inspect` 登记检查，不根据脚本生成成功自动填写通过。
4. 五图通过后 `render` 打包，顺序固定为 `cover/card_0/card_1/card_2/card_3`。
5. 用最新 `content_hash/source_hash/manifest_hash` 登记最终审阅。

`examples/content.json` 是四卡离线演示；`examples/native-content.json` 仅展示五图协议，必须先取得实际生成结果，不能直接当作完成素材。

## 可选发布

发布需要独立服务、实际账号绑定、来源核查、素材最终审阅、有效编辑器会话和显式启用的策略。`backend-preflight` 保留 15 分钟实际会话；实际看图并核对后，用 `backend-review` 登记与当前会话和内容匹配的审阅。

`backend-submit` 在外部动作之前持久保存唯一提交意图，并最多调用一次提交。提交返回值、点击成功、按钮消失或提示信息都不证明已发布。`backend-management` 和 `backend-observations` 保存实际管理证据与未验证候选，再由执行者核对唯一笔记、平台明确状态、正文和图序，通过 `reconcile` 登记。

`submit_unknown` 需要先回查，禁止自动重复提交。测试夹具不能授权真实发布。修改内容或图片会清除旧审阅；旧会话不能用于新内容。

## 暂停与备份

`pause` 停止新采集和提交，已有提交仍可只读回查。`resume` 不清空账本。`backup --output <新文件绝对路径>` 创建一致性 SQLite 备份并拒绝覆盖已有文件。账号凭据和浏览器会话另行保存，不随开源代码导出。

定时运行由外部调度器或 Codex 应用配置，项目没有隐藏的定时启动、登录或发布程序。将现有自动化迁移到新机器时，需重新配置调度及授权。
