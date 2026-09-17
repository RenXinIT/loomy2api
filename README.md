# loomy2api

把 **Loomy（讯飞）Web 版** 封装成一个 **OpenAI 兼容 API**，并支持**多账号额度池**、
**额度感知负载均衡**、**会话粘性**、**自动故障转移**与**浏览器登录管理**。

> 一个 Base URL + 一个 API Key + 统一模型列表 —— 后端自动在多个 Loomy 账号之间调度。

```
CC Switch / OpenClaw / 任意 OpenAI 客户端
                 │  单 Base URL + 单 API Key
                 ▼
           loomy2api 反代
                 │  账号池 · 额度缓存 · 选号 · 故障转移 · 会话粘性
                 ▼
      [ 账号A   账号B   账号C … ]
                 ▼
          Loomy 上游 (loomy.xunfei.cn)
```

---

## 目录

- [特性](#特性)
- [快速开始](#快速开始)
- [配置](#配置)
- [账号池](#账号池)
- [增加账号](#增加账号)
- [管理界面与 API](#管理界面与-api)
- [调用 API](#调用-api)
- [模型](#模型)
- [数据与持久化](#数据与持久化)
- [安全](#安全)
- [架构](#架构)
- [已知限制](#已知限制)
- [故障排查](#故障排查)
- [目录结构](#目录结构)

---

## 特性

| 能力 | 说明 |
|---|---|
| OpenAI 兼容 | `POST /v1/chat/completions`（流式 SSE + 非流式）与 `GET /v1/models` |
| 多账号池 | 一个 base URL + 一个 key，后端起池调度，账号对客户端完全透明 |
| 额度感知 | 定时拉取各账号额度，按额度加权分流 |
| 负载均衡 | 平滑加权轮询（SWRR），权重 = 额度 × 健康 × 空闲 ÷ 并发 |
| 会话粘性 | 同一会话钉住同一账号，避免上游上下文丢失 |
| 故障转移 | 按错误类型分类处理：限流软冷却、5xx 熔断、鉴权失效不重试 |
| 额度监控 | `GET /credit` 缓存快照，供监控/技能只读消费 |
| 浏览器登录 | 容器内 Chromium + noVNC，扫码/短信登录后自动保存凭证 |
| 管理界面 | `/admin` 可视化管理账号、查看额度与健康、查看最近错误 |
| 脱敏 | 日志与界面**绝不**输出 Key / Cookie / Token / Authorization |
| 单账号兼容 | 只有一个账号时，行为与单账号版本一致 |

---

## 快速开始

### 前置条件

- Docker + Docker Compose v2
- 一个可用的 Loomy 账号（手机号登录）
- 建议 2 GB 以上可用内存（镜像内含 Chromium）

### 1. 准备配置

```bash
git clone https://github.com/wangliangdong/loomy2api.git
cd loomy2api

cp .env.example .env
vi .env          # 至少设置 LOOMY_API_KEY 与 PUBLIC_HOST
chmod 600 .env
```

### 2. 启动

```bash
docker compose up -d --build
```

首次构建会安装 Chromium 与 Xvfb/noVNC，耗时较久。构建完成后：

```bash
docker compose ps
curl http://127.0.0.1:7865/health
```

健康响应示例：

```json
{
  "status": "ok",
  "service": "loomy2api",
  "version": "1.3.0",
  "loomy_session_configured": false,
  "api_key_enabled": true,
  "model_count": 10,
  "pool_mode": "single",
  "accounts_total": 1,
  "accounts_usable": 1,
  "credit_refresh_interval": 45
}
```

### 3. 登录 Loomy

浏览器打开：

```
http://<PUBLIC_HOST>:7865/login
```

页面会给出 noVNC 链接，点开完成登录（手机号 + 短信验证码）。
登录成功后凭证会自动保存到该账号，无需手工复制 Cookie。

验证：

```bash
curl http://<PUBLIC_HOST>:7865/auth/status
```

### 4. 调用

```bash
curl -N \
  -H "Authorization: Bearer <LOOMY_API_KEY>" \
  -H "Content-Type: application/json" \
  http://<PUBLIC_HOST>:7865/v1/chat/completions \
  -d '{
    "model": "GLM-5.3-Flash",
    "messages": [{"role": "user", "content": "你好，请回复一句测试成功"}],
    "stream": true
  }'
```

---

## 配置

全部通过环境变量（`.env`）。完整清单见 [`.env.example`](.env.example)。

### 必填

| 变量 | 说明 |
|---|---|
| `LOOMY_API_KEY` | 客户端调用时需携带的 Bearer Key。生成：`openssl rand -hex 24` |
| `PUBLIC_HOST` | 浏览器可达的主机名/IP，用于拼接 noVNC 链接 |

### 服务

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `7865` | API 监听端口 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `DATABASE_PATH` | `/app/data/loomy2api.db` | 会话↔上游会话 ID 映射库 |
| `SESSION_STATE_PATH` | `/app/data/loomy-storage-state.json` | 旧版单账号凭证路径（向后兼容） |

### 上游

| 变量 | 默认 | 说明 |
|---|---|---|
| `LOOMY_WEB_URL` | `https://loomy.xunfei.cn/web` | 登录/控制台页面 |
| `LOOMY_API` | `https://loomy.xunfei.cn/web/api/chat/completions` | 聊天补全端点 |
| `LOOMY_BASE_URL` | `https://loomy.xunfei.cn` | 额度/资料接口基地址 |

### 管理

| 变量 | 默认 | 说明 |
|---|---|---|
| `ADMIN_TOKEN` | 空 | 管理写操作口令。留空则回退用 `LOOMY_API_KEY`；两者都空则**拒绝所有写操作**（403） |

### 账号池

| 变量 | 默认 | 说明 |
|---|---|---|
| `CREDIT_REFRESH_INTERVAL` | `45` | 额度刷新间隔（秒） |
| `CREDIT_REFRESH_GAP` | `0.8` | 刷新各账号之间的间隔（秒，避免触发限流） |
| `POOL_MAX_IN_FLIGHT` | `3` | 单账号最大并发请求数 |
| `POOL_BREAKER_THRESHOLD` | `3` | 连续上游错误达到此值开启熔断 |
| `POOL_BREAKER_COOLDOWN` | `1800` | 熔断基础冷却秒数（每次叠加翻倍） |
| `POOL_BREAKER_COOLDOWN_MAX` | `21600` | 熔断冷却上限 |
| `POOL_SOFT_RATE_COOLDOWN` | `600` | HTTP 429 后的基础冷却秒数（叠加翻倍） |
| `POOL_SOFT_RATE_COOLDOWN_MAX` | `7200` | 限流冷却上限 |
| `POOL_QUOTA_FLOOR` | `0.15` | 额度权重下限，避免低额度账号被完全饿死 |
| `POOL_IDLE_WEIGHT_PER_HOUR` | `0.5` | 空闲账号每小时增加的权重（公平性） |
| `POOL_IDLE_WEIGHT_MAX` | `5.0` | 空闲权重上限 |
| `POOL_STICKY_ENABLED` | `true` | 会话粘性开关 |
| `POOL_STICKY_TTL` | `1800` | 粘性绑定有效期（秒） |
| `POOL_STICKY_GC_INTERVAL` | `300` | 粘性记录回收间隔（秒） |
| `POOL_FAILOVER_ENABLED` | `true` | 跨账号故障转移开关 |
| `POOL_FAILOVER_ATTEMPTS` | `3` | 单请求最大尝试次数 |
| `POOL_FAILOVER_DEADLINE` | `20.0` | 单请求故障转移总时间预算（秒） |
| `POOL_STATE_SAVE_INTERVAL` | `30` | 运行状态落盘间隔（秒） |
| `DATA_DIR` | `/app/data` | 注册表与凭证文件的根目录 |

---

## 账号池

### 选号流程

**1. 硬过滤** —— 以下账号直接剔除：

```
已禁用 ‖ 鉴权失效 ‖ 额度耗尽 ‖ 冷却中 ‖ 并发已达上限
```

**2. 加权** —— 对剩余账号计算权重：

```
权重 = 额度份额 × 健康度 × 空闲度 ÷ (1 + 当前并发)
```

- **额度份额**：相对池内最高额度归一，并施加 `POOL_QUOTA_FLOOR` 下限
- **健康度**：`(1 + 成功数) / (1 + 成功数 + 3 × 错误数)`
- **空闲度**：距上次成功越久权重越高（上限 `POOL_IDLE_WEIGHT_MAX`）

**3. 选择** —— 平滑加权轮询（SWRR），分配平滑无突发。

### 会话粘性

Loomy 的会话 ID **归属于单个账号**。若同一会话中途换号，上游上下文会丢失。
因此默认开启粘性：会话首次选定账号后钉住 `POOL_STICKY_TTL` 秒。

特例：若钉住的账号**仅因并发满**而暂不可用，请求会临时路由到别处，但
**保留原钉住关系**——避免会话被永久迁走。

### 故障转移与错误分类

| 上游信号 | 分类 | 处理 |
|---|---|---|
| `401` / `403` / 含 `UNAUTHENTICATED` / `请先登录` | `FATAL_AUTH` | **不跨账号重试**，标记该账号需重登 |
| `429` / 含 `rate limit` | `RETRY_RATE` | 软冷却（`POOL_SOFT_RATE_COOLDOWN`，叠加翻倍） |
| `5xx` / `408` / `409` / `425` | `RETRY_UPSTREAM` | 连续达阈值打开熔断，冷却后自动恢复 |
| 其他 | `CLASS_NONE` | 仅记录 |

> **为什么鉴权失效不重试**：Loomy **不签发 refresh token**，Cookie 失效只能
> 人工重新登录。反复重试只会浪费配额并掩盖真实问题。

### 单账号 vs 多账号

按**已注册账号数**判定（不是启用数）：

- **恰好 1 个账号** → 单账号路径。行为与旧版一致，会话键沿用 `session:...` 形状，
  升级对磁盘零变化。
- **≥ 2 个账号** → 池模式。会话键变为 `acct:{id}:session:...`，并启用上述调度逻辑。

---

## 增加账号

推荐流程（**务必按顺序**，中途别发请求）：

### 1. 拿到新账号的 Cookie

用**你自己的浏览器**（建议无痕窗口）打开 `https://loomy.xunfei.cn/web`，
用第二个账号登录。然后：

DevTools → Application → Cookies → `https://loomy.xunfei.cn` → 复制 `loomy_web_session` 的值。

或在 Console 执行：

```js
document.cookie.split('; ').filter(c => c.startsWith('loomy_web_session')).join('; ')
```

### 2. 在管理页新增账号

打开 `http://<PUBLIC_HOST>:7865/admin`

- 「管理口令」填 `ADMIN_TOKEN`（未配置则填 `LOOMY_API_KEY`），点**保存**
- 「新增账号」填名称，点**添加账号**

### 3. 立即写入 Cookie

点该账号行的**凭据** → 粘贴 → **保存并验证**。

保存后会立即验证并查询额度。出现「验证成功，额度 N」即完成。

### 命令行等价

```bash
KEY=<LOOMY_API_KEY>

# 新增
curl -s -X POST http://<host>:7865/admin/api/accounts \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"label":"副号"}'

# 写入 Cookie
curl -s -X POST http://<host>:7865/admin/api/accounts/<acc_id>/cookie \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"cookie":"loomy_web_session=..."}'

# 查看
curl -s http://<host>:7865/admin/api/accounts | python3 -m json.tool
```

> ⚠️ **加第二个账号会立刻把部署切到池模式**（见上文）。这是设计行为。

---

## 管理界面与 API

### 页面

| 路径 | 说明 |
|---|---|
| `/` | 首页：账号概览与入口 |
| `/admin` | 账号管理界面 |
| `/credit` | 额度快照（JSON） |
| `/login` | 启动登录浏览器 |
| `/login/console` | 登录指引与 noVNC 链接 |
| `/auth/status` | 登录状态 |
| `/health` | 健康检查 |

### 管理 API

写操作需 `X-Admin-Token: <ADMIN_TOKEN>` 或 `Authorization: Bearer <LOOMY_API_KEY>`。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/admin/api/accounts` | 列出账号（只读，无需口令） |
| `POST` | `/admin/api/accounts` | 新增账号 `{"label":"…"}` |
| `DELETE` | `/admin/api/accounts/{id}` | 删除账号 |
| `POST` | `/admin/api/accounts/{id}/enabled` | 启用/禁用 `{"enabled":true}` |
| `POST` | `/admin/api/accounts/{id}/cookie` | 写入凭证 `{"cookie":"…"}` |
| `POST` | `/admin/api/accounts/{id}/refresh` | 刷新单账号额度 |
| `POST` | `/admin/api/refresh` | 刷新全部额度 |

### 账号状态

| 状态 | 含义 |
|---|---|
| `ok` | 正常 |
| `disabled` | 已手动禁用 |
| `needs_relogin` | 凭证失效，需重新登录 |
| `exhausted` | 额度耗尽 |
| `rate_limited` | 限流冷却中 |
| `cooling` | 上游错误熔断冷却中 |
| `degraded` | 额度查询失败（保留上次数值，仍可用） |

### `/credit` 输出

只读缓存，**不会**因为监控调用而访问上游（除非显式 `?refresh=1`）。

```json
{
  "service": "loomy2api",
  "accounts": [
    {
      "id": "acc_main",
      "label": "主号",
      "phone": "138****0000",
      "nickname": "example-user",
      "remain": 5000,
      "daily": 4877,
      "healthy": true,
      "state": "ok",
      "auth_state": "ok",
      "rate_limited": false,
      "enabled": true,
      "inflight": 1,
      "requests": 120,
      "success": 118,
      "errors": 2,
      "quota_query_fail": 0,
      "checked_at": 1789556145,
      "checked_age": 3.2,
      "cooldown_remaining": 0,
      "last_error": null
    }
  ],
  "total": { "accounts": 1, "enabled": 1, "healthy": 1, "remain": 5000 },
  "ts": 1789556145,
  "queried_at": 1789556145
}
```

---

## 调用 API

### 会话隔离

代理为每个逻辑会话维护独立的 Loomy 上游会话。会话 ID 按以下优先级解析：

1. `x-openclaw-session-key` —— OpenClaw 网关调用首选
2. `X-Session-ID` —— 通用客户端/会话头
3. `X-CC-Switch-Session-ID` —— CC Switch 集成
4. OpenAI `user` 字段 —— 兜底
5. 由 agent + system/developer 消息 + 首条用户消息计算的稳定指纹 —— 兜底

同一会话/模型的上游会话 ID 会被串行化，避免并发轮次互相覆盖。

### 身份传递

Loomy Web 的接口**只有一个 `content` 字段**，没有标准的 role 数组。
代理会把 OpenAI 的 `system` / `developer` 消息与工作区文件注入到新会话的首轮用户消息中，
使模型以调用方期望的身份作答，而非 Loomy 的默认人设。

> 这是**适配**而非真正的 system-role 翻译。

### 在 OpenClaw / CC Switch 中配置

作为 OpenAI 兼容 provider 添加：

| 项 | 值 |
|---|---|
| Base URL | `http://<host>:7865/v1` |
| API Key | 你的 `LOOMY_API_KEY` |
| API | OpenAI Chat Completions / OpenAI-compatible |
| Models | 使用 `/v1/models` 返回的 ID |

---

## 模型

`/v1/models` 返回 **10 个规范模型 ID**（去重、不暴露账号前缀、不含别名行）：

```
deepseek-v4-flash-0731
MiniMax-M3
Kimi-k2.6
qwen-3.8-max
qwen3.8-flash
GLM-5.3-Flash
spark-x
doubao-seed-2.0-mini
mimo-v2.5
qwen3.5-flash
```

别名（如 `kimi`、`glm-5.3-flash`、`doubao`）可用于请求，但**不会**出现在 `/v1/models` 中。
完整别名表见 [`config.py`](config.py)。

---

## 数据与持久化

`./data` 是唯一持久化卷：

| 文件 | 内容 |
|---|---|
| `loomy2api.db` | 会话键 → 上游会话 ID 映射 |
| `accounts.json` | 账号注册表 |
| `accounts/{id}.json` | 各账号凭证（storage state，含 Cookie） |
| `pool_state.json` | 池运行状态（额度、计数、冷却、粘性） |
| `loomy-storage-state.json` | 旧版单账号凭证路径（向后兼容） |

**备份**：只需备份整个 `./data` 目录。**其内容等同于账号凭证，切勿提交或外传。**

---

## 安全

- **`.env` 与 `data/` 绝不提交**（`.gitignore` 已覆盖）
- 日志与界面**不输出** Key / Cookie / Token / Authorization / 明文手机号
- `data/` 中的凭证文件建议 `chmod 600`
- **6080 端口（noVNC）会驱动一个已登录的浏览器会话**，务必限制在 LAN/VPN，
  绝不可暴露公网
- 若需对外提供 7865，请置于带鉴权的反向代理之后
- 未配置 `ADMIN_TOKEN` 且未配置 `LOOMY_API_KEY` 时，管理写操作一律拒绝

---

## 架构

```
┌─ loomy2api ─────────────────────────────────────────────┐
│  routes:  /v1/chat/completions   /v1/models             │
│           /credit  /admin                               │
│                                                          │
│  ┌────────────┐  ┌──────────┐  ┌───────────┐            │
│  │ AccountPool│  │ Selector │  │  Credit   │            │
│  │ 注册表+状态 │─►│ 选号算法  │  │  Cache    │            │
│  └────────────┘  └──────────┘  └───────────┘            │
│        ▲               ▲              ▲                  │
│  ┌───────────┐   ┌───────────┐  ┌──────────────┐        │
│  │ Sticky    │   │ Cooldown  │  │  Refresher   │        │
│  │ Store     │   │ Breaker   │  │ (定时 45s)    │        │
│  └───────────┘   └───────────┘  └──────────────┘        │
└──────────────────────────────────────────────────────────┘
```

**额度获取与路由解耦**：Refresher 定时拉取额度写入缓存；
Selector **只读缓存**，绝不在请求路径上访问上游额度接口。

### 模块

| 文件 | 职责 |
|---|---|
| `app.py` | FastAPI 路由、SSE 转发、会话解析、故障转移 |
| `accounts.py` | 账号池：注册表、状态、选号、健康记录、持久化 |
| `credit.py` | 额度刷新与缓存、账号资料 |
| `auth_browser.py` | 容器内 Chromium 登录与凭证保存 |
| `loomy_client.py` | 上游 HTTP 客户端与 SSE 解析 |
| `config.py` | 配置与模型表 |
| `database.py` | 会话映射存储 |
| `models.py` | 请求模型 |

---

## 已知限制

- **无工具调用**：Loomy Web 接口只接受单个 `content` 字段，没有 `tools` 位。
  OpenAI `tools` 会被接受但不向上游翻译，也不返回 `tool_calls`。
  需要工具调用请使用支持该能力的其他 provider。
- **不翻译采样参数**：`temperature`、`max_tokens` 不向上游传递。
- **`reasoning_effort`**：接受但有意不翻译（上游字段未确认）。
- **切号丢上下文**：Loomy 会话 ID 归属单账号，这是上游设计决定的固有限制；
  会话粘性用于降低其影响，但账号不可用时仍会切换。
- **Cookie 需人工续期**：Loomy 不签发 refresh token，失效后只能重新登录。
- **`daily` 字段语义**：已采集但未确认精确含义，因此**不参与路由**。

---

## 故障排查

### 构建很慢 / 失败

镜像基于 `mcr.microsoft.com/playwright/python`，首次构建需下载 Chromium。
确保磁盘与网络充足。

### `/health` 返回 `loomy_session_configured: false`

尚未登录。访问 `/login` 完成登录。

### 调用返回 503

```
Loomy is not logged in. Open /login first.
```

该账号无凭证，或凭证已被判定失效。重新登录或用 `/admin` 写入 Cookie。

### 账号显示 `needs_relogin`

Cookie 失效。Loomy 无 refresh token，必须重新登录。

### 调用返回 400

- `Unsupported model: …` —— 模型名不在别名表中
- `No user message found` —— 请求中没有有效的用户消息

### 流式响应中途断流

代理在首字节前检测到空响应会自动换会话重试（最多 3 次）。
若仍失败，检查上游账号额度与登录状态。

### 增加账号后行为变化

这是设计行为：账号数 ≥ 2 即进入池模式（见 [单账号 vs 多账号](#单账号-vs-多账号)）。

---

## 目录结构

```
loomy2api/
├── app.py               # FastAPI 应用与路由
├── accounts.py          # 账号池与选号
├── credit.py            # 额度刷新与缓存
├── auth_browser.py      # 浏览器登录
├── loomy_client.py      # 上游客户端
├── config.py            # 配置与模型表
├── database.py          # 会话映射存储
├── models.py            # 请求模型
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── data/                # 运行时数据（不提交）
```

---

## 许可

仅供个人学习与自用。使用请遵守 Loomy / 讯飞的服务条款。
本项目与 Loomy、科大讯飞无任何隶属关系。
