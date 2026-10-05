# 可选 WeRSS 接入

本目录提供本项目的引导脚本、严格刷新端点和 WeRead 最新一篇正文验证。上游镜像固定为：

```text
ghcr.io/rachelos/we-mp-rss@sha256:af771f21b3f7958a5dea16911fba050a6d7b92eac2fb2499c467c1b11f07ef34
```

镜像由 [rachelos/we-mp-rss](https://github.com/rachelos/we-mp-rss) 提供，上游 MIT 许可附在 `UPSTREAM_LICENSE`。本项目不复制镜像、浏览器、账号授权或数据库。

在 Docker 可用的 Windows PowerShell 中，`deploy.ps1` 可创建独立状态目录和随机管理员凭据。默认容器名 `xhs-werss-local`、回环端口 `8002`；已有容器、状态目录或被占用端口会停止操作，不覆盖现有部署。

```powershell
./integrations/werss/deploy.ps1
```

随机凭据保存在 `Codex/state/we-mp-rss-local/admin-credentials.json`，请在本机受控查看并登录服务，完成自己的上游授权、来源绑定和独立验收。脚本不替用户扫码或承诺上游持续可用。

Python 来源配置的 `refresh` 字段需要 `type: werss`、`base_url`、真实 `mp_id`、私有 `credentials_path`、`start_page: 0`、`end_page: 1`。WeRead 模式还需 `acquisition_mode: weread_mp` 和该来源真实 `weread_book_id`。刷新结果包含当前能力和覆盖证明；最新一篇不视为历史补抓或完整多篇覆盖。

管理员凭据、Cookie、上游响应和数据库都属于运行状态，不能写进例子或提交仓库。停止 / 删除容器应由部署者根据实际状态执行，源码改造不触碰原有生产容器。
