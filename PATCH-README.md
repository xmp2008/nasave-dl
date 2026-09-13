# nasave-dl 多订阅 + 多平台扩展

闭源免费版 nasave-dl 原生限制 **只能建 1 个订阅**（前端按钮锁 + 后端 `POST /api/subs` 硬 403）。
本仓库在不修改官方二进制的前提下，通过 **UI 覆盖 + 旁路服务** 解锁多订阅、增强多平台识别。

> 多平台下载能力本身官方已具备（yt-dlp 支持数千视频站 + gallery-dl 支持数百图集站），
> 本扩展主要解锁「订阅数量」，并补全 UI 平台徽章识别与表单提示。

## 原理

```
浏览器 UI (sync.html 补丁)
   │  新建订阅 ──→ :8890/bypass/subs      (subs_bypass.py 旁路服务，直写 SQLite)
   │  列表/编辑/开关/同步 ──→ :8889/api/*  (官方 API，实测无门槛)
   ▼
官方引擎 (main.*.so，未动一个字节)
   │  定时器/同步器直接读 subscriptions 表 → 自动感知旁路创建的行
   ▼
yt-dlp / gallery-dl 下载
```

- **sidecar 直写共享 SQLite（WAL）**：与官方进程并发安全（30s 锁等待 + 短事务）。
  官方定时调度器与同步 API 对旁路写入的行**完全接管**（已实测：列表/PUT 编辑/触发同步全部 200）。
- **删除无门槛**：官方 `DELETE /api/subs/{id}` 不限 Pro；批量删除前端改为逐个 DELETE
  （官方 batch 接口只支持暂停/恢复，删除会 400，顺手修了这个坑）。
- **创建后自动回调 `POST /api/subs/{id}/sync`** 触发首次抓取，失败不阻塞（定时器兜底）。

## 文件

| 文件 | 用途 |
|---|---|
| `overlay/web/sync.html` | 官方 `web/sync.html` 的补丁版（bind 挂载覆盖，移除即还原官方） |
| `subs_bypass.py` | 旁路服务（纯标准库，`--selftest` 可自检） |
| `docker-compose.yml` | 测试/生产 stack（含旁路服务定义） |

`sync.html` 改动点（均可 grep 验证）：
1. `新建订阅`按钮解除 `isPro && subs.length >= 1` 锁，按钮显示当前数量。
2. `saveSub()` 新建分支改走 `/bypass/subs`（编辑仍走官方 PUT）。
3. `formatSite()` 扩容：抖音/快手/小红书/微博/Pixiv/AcFun 等（原仅 6 家）。
4. 订阅链接输入框 placeholder 列明支持平台。
5. `batchAction('delete')` 改走逐个 DELETE（官方 batch 不支持删除）。

## 部署（QNAP + Portainer stack）

前置：宿主机目录里放好 `subs_bypass.py`，`web/sync.html`（补丁版）。

```yaml
# 关键卷映射见 docker-compose.yml：
#   /app/data  → 共享 SQLite（两个容器同一目录，WAL 并发安全）
#   /app/web/sync.html → ro 覆盖官方 UI
#   /downloads → 下载目录
```

- 主程序端口：官方 `run.py` 写死 8888（`PORT` 环境变量**无效**），
  因此用独立网段 + `8889:8888` 映射，与生产 host 网络实例（8888）并存不冲突。
- 旁路端口：`8890`（需与 `sync.html` 中 `:8890` 一致）。

## 已验证（E2E 12/12）

UI 覆盖生效 / 旁路健康 / 连创 3 个不同平台订阅 / 官方 API 全部列出 /
官方 PUT 编辑旁路行 / 官方触发同步 / 旁路删除 / 计数一致。

## 局限与注意

- 官方升级镜像后，若 `sync.html` 模板结构大改，需重新适配补丁（bind 覆盖与官方文件同路径，直接可见）。
- `network.html` 的通知推送（Bark/ServerChan）Pro 锁**不在本扩展范围**，保持未动。
- Pro 徽章 / 激活入口保持原样（未伪造 Pro 状态）。
- 仅供个人 NAS 学习使用，请支持原作者的 Pro 授权。
