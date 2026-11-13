# Policy Intelligence Agent

## 项目简介

面向个人使用的政策趋势分析助手：从政府网站采集政策文件，清洗正文，调用兼容 OpenAI 接口的模型进行结构化分析，并生成 Markdown 报告、通过可选渠道发送通知。

> 项目定位是信息整理与分析工具，不构成投资、就业、法律或政务决策建议。政策原文、数据来源与模型输出都应人工复核。

## 功能概览

- 采集国家级、省级和市级政策来源，来源通过 `config.yaml` 配置；默认包含国务院、湖北省、武汉市、十堰市、人社部。
- 使用 Requests、BeautifulSoup、Scrapling 和 Playwright 处理常规页面与需要浏览器渲染的页面；湖北、武汉来源可通过 Tavily 增强发现。
- 使用 SQLAlchemy + Alembic 管理 SQLite/PostgreSQL 数据；当前表结构覆盖政策、分析、任务、通知、会话和数据源健康状态。
- 使用 OpenAI 兼容 API 生成结构化 JSON 分析，兼容 OpenAI、DeepSeek、通义千问等服务。
- 生成日报、周报、月报、季报 Markdown 文件；支持交互式 CLI 与企业微信智能机器人对话。
- 通过邮件、飞书群机器人、企业微信群机器人发送报告；通知默认关闭，必须显式配置。
- 提供 FastAPI `/health` 探针、Scheduler、飞书/企业微信事件回调和数据源健康诊断能力。

### 效果与实际验证

以下素材来自 2026-11-12 至 2026-11-13 的脱敏验证：真实 AI 分析记录、来源运行链路巡检、来源结构探针，以及企业微信、邮件实发台账。它们用于说明当前效果，**不代表全量历史、持续稳定性或生产 SLA**。当前 `config.yaml` 的 5 个启用来源已完成本地连续两轮和 GitHub Actions 手动 5/5 验收；Release Smoke Check 也已实际覆盖 PostgreSQL、Docker Compose 和健康检查。尚无 `event=schedule` 的 14 天连续记录，因此仓库仍保持 Private；飞书和通知效果截图仍未完成真实验收。

#### 真实 AI 分析样例

![真实 AI 分析样例：电力安全事故应急处置和调查处理条例](docs/assets/ai-analysis-sample.png)

样例记录：`policy_id=9`、`analysis_id=8`、模型 `auto`、全文 6,740 字符、耗时 27.31 秒、趋势评分 88；分析状态为成功并标记为可信结果。AI 输出仅供参考，须结合原文人工复核。

#### 真实日报内容预览

![真实日报内容预览：一句话总结、政策大白话、产业影响与大学生建议](docs/assets/daily-report-preview.png)

此图使用上述真实落库分析内容整理，不展示数据库路径、URL、账号或 Token。它是内容形态预览，不能替代生产环境通知投递截图。

#### 一次五来源实际采集量

![五个来源本次实际采集量：合计 69 条](docs/assets/source-yield.png)

一次真实 `fetch()` 调用中，五个来源均返回 `ok`，合计取得 69 条；其中国务院 29、湖北省 20、武汉市 7、十堰市 5、人社部 8。该图是采集量样本；后续 `source-canary` 又连续两轮验证了每个启用来源的列表与首条正文。单次成功不等于持续稳定，也不代表逐条页面正文都已验证。

#### 来源结构健康诊断

![五个来源结构健康诊断：正常 2，WAF 拦截 2，未配置 selector 1](docs/assets/source-health.png)

结构探针结果为正常 2/5、WAF 拦截 2/5、未配置 selector 1/5。**“实际调用返回 ok”与“结构健康”是两个不同口径**：本次没有绕过 WAF，也未把异常结构统计为采集成功。结构探针用于诊断固定选择器和页面形态；`source-canary` 则验证包含同源 JSON、浏览器和搜索辅助在内的实际运行链路，两套结果不能互相替代。

#### 趋势评分样本

![七条已保存分析的匿名趋势评分样本](docs/assets/trend-sample.png)

展示 7 条已保存分析的趋势评分，平均 83.4、中位数 82。旧记录中的部分标题存在编码异常，因此只展示匿名样本编号，不将乱码标题或内容当作有效展示材料。该图是样本分布，不是全量政策趋势图。

### 系统架构

```mermaid
flowchart LR
    A[政府网站 / 搜索服务] --> B[来源采集与正文清洗]
    B --> C[SQLite / PostgreSQL]
    C --> D[OpenAI 兼容模型]
    D --> C
    C --> E[Markdown 报告]
    E --> F[本地 reports/]
    E --> G[邮件 / 飞书 / 企业微信通知]
    C --> H[FastAPI /health]
    S[APScheduler] --> B
    S --> D
    S --> E
    C --> I[消息机器人与事件回调]
```

系统不提供 Web 管理后台；日常操作通过 CLI、Scheduler 和消息机器人完成。FastAPI 主要用于健康探针和事件回调。

### 数据流转

```mermaid
flowchart TD
    A[来源发现] --> B[列表抓取与日期过滤]
    B --> C[正文抓取与清洗]
    C --> D[去重与重要性评分]
    D --> E[Policy 落库]
    E --> F[结构化 AI 分析]
    F --> G[Analysis 落库]
    G --> H[日报 / 周报 / 月报 / 季报]
    H --> I[报告落盘与可选通知]
    F --> J[失败记录与重试]
    I --> K[通知状态记录]
```

#### 通知效果展示（待补充）

[此处需补充：企业微信/邮件通知效果截图]
建议展示：一条脱敏后的日报通知、报告标题和公开可追溯链接。不要展示真实群成员、手机号、Webhook、企业 ID、Token 或内部链接。2026-11-13 已实际完成企业微信 2 个分片和邮件 1 个分片的发送，并通过通知台账查询确认 `success`；但截图仍待人工补充，不能用台账文字或模拟界面代替。Docker 部署截图不作为项目介绍页的必需展示项。

## 当前验证状态与发布边界

以下状态记录于 **2026-11-13**，用于区分“已经证明什么”和“尚未证明什么”。未执行的项目不会被写成通过。“长期可用”在本项目中定义为：`config.yaml` 中全部 5 个启用来源每日自动巡检，真实取得非空列表并验证首条正文；任一来源失败、缺失或名称变化，任务必须失败。

| 验证项 | 当前状态 | 证据或剩余条件 |
|---|---|---|
| 全新 Python 环境安装 | 已通过 | 在 Python 3.12.12 的隔离虚拟环境中执行 `pip install -r requirements.txt` 与 `pip check`；GitHub Actions 另用全新 Python 3.11 完成依赖安装、`pip check`、SQLite 初始化和健康检查。 |
| SQLite 初始化与迁移 | 已通过 | 使用隔离 SQLite 执行 `python main.py init`，Alembic 版本为 `91a4d2f7c8b0 (head)`。 |
| FastAPI 健康检查 | 已通过 | Uvicorn 实际启动后，`GET /health` 返回 HTTP 200；数据库、Scheduler 和 Agent 状态正常。 |
| Release Smoke Check | 已通过 | GitHub Actions Run `43430587587` 在全新 Ubuntu、Python 3.11、PostgreSQL 16 和 Docker Compose 环境中成功；其中 Docker 任务实际执行 `docker compose up --build --detach`，容器达到 `healthy` 并通过 `/health`。 |
| 当前启用来源手动 canary | GitHub Actions 5/5 通过（仅手动运行） | Run `43430807699`、Artifact `14040291339`：国务院 29/12000、湖北省 20/7947、武汉市 5/6661、十堰市 5/2851、人社部 6/6030（列表数/首条正文字符数）。本地同时保留 2026-11-13 基线结果。手动运行**不计入**连续天数验收。 |
| AI 分析 | 已通过（受控样本） | 在隔离 SQLite 环境中真实采集全部 5 个启用来源并取得 69 条；受控选取 1 条送入流水线，由兼容模型完成全文结构化分析，趋势评分 72，生成日报并落库。该结果只证明受控样本链路。 |
| 真实通知投递 | 企业微信、邮件已通过；飞书未验证 | 2026-11-13 实际发送企业微信 2/2 分片、邮件 1/1 分片，均无重试，台账状态为 `success`。飞书未配置且未实发，不能记为通过。 |
| 来源巡检自动化 | 已配置并通过手动 Actions 验证 | `.github/workflows/source-canary.yml` 每日 00:17 UTC（北京时间 08:17）运行，使用固定 5 来源名称、数量、列表数和正文阈值门禁；仅上传脱敏 Manifest。Tavily 是推荐增强项，不是五来源运行的硬依赖。 |
| 14 个北京时间自然日连续验收 | 未完成 | 截至 2026-11-13，尚无可计入的 `event=schedule` 运行；Release Gate Run `43431107930` 返回 `complete=false`、`passed_days=0`，符合当前证据不足的预期。只有每日至少一次定时运行按固定 5 来源门禁成功，才计为当天通过；失败、缺失或来源不完整会重置连续计数。 |
| 敏感信息审计 | 已通过当前 HEAD | 对当前提交和全部可达 Git blob 复扫，未发现高危凭据格式或实际非空配置值的历史命中；最终公开前仍需对发布提交重跑。 |
| 可公开性 | 被 14 天连续验收阻塞 | 当前为单根 Private 仓库，跟踪树未包含测试、数据库、日志、报告或缓存路径。连续验收、最终对象审计和公开后匿名核验完成前，不得切换为 Public。 |

详细逐日记录见 [`docs/source-canary-acceptance.md`](docs/source-canary-acceptance.md)。如果 2026-11-14 的首个定时任务及此后每日任务均通过，最早可在 **2026-11-27** 完成 14 个北京时间自然日窗口；任何一天失败或缺失，完成日期必须相应顺延。

### 发布路径结论

当前 GitHub 仓库已经是经过手动 Actions 验证的 **Private 单根仓库**，不再需要重建发布快照。公开发布顺序为：

1. 从 2026-11-14 起逐日核对 `event=schedule` 的 Run ID、Artifact ID 和 5 个来源结果，并写入验收台账。
2. 完成连续 14 个北京时间自然日后，重跑 Release Smoke Check、手动 Canary 和全历史敏感信息审计。
3. 删除远端 `codex/*` 临时分支，确认远端仅保留 `main` 且无标签。
4. 将仓库切换为 Public，再通过匿名请求确认 `private: false`、README 和工作流文件可访问。

任何一步失败都停留在 Private。外部政府网站改版、WAF 策略变化或区域网络中断无法被代码永久消除；长期可用性只能通过持续巡检、失败告警和及时修复来维持。

### 发布审计命令

在具备仓库读取权限的环境中设置 `GITHUB_TOKEN` 后，审计 14 个北京时间自然日的定时 Canary：

```powershell
python scripts/audit_canary_history.py `
  --repo koiXiaoze/policy-ai `
  --token-env GITHUB_TOKEN `
  --output reports/canary-history-audit.json
```

对最终提交、全部可达 Git 对象和全部 `origin` 分支复扫敏感内容，并使用脱敏环境模板核对实际配置值：

```powershell
python scripts/audit_public_release.py `
  --all-remote-refs `
  --env-file .env.example `
  --json-output reports/public-release-audit.json
```

两个脚本的退出码均为：`0` 表示完整通过；`1` 表示证据不足或安全门禁失败；`2` 表示 API、格式、网络或报告写入异常。Canary 审计仅在 14 天证据全部通过时才在 `earliest_release_date` 返回已审计窗口结束日；空窗口或部分窗口返回 `null`，不会根据当前日期推测未来发布日期。自动 Release Gate 每日 01:17 UTC（北京时间 09:17）审计截至昨日的 14 天窗口。若 2026-11-27 当天的 Canary 已完成且需要将当天计入窗口，应在当日手动指定窗口结束日：

```powershell
python scripts/audit_canary_history.py `
  --repo koiXiaoze/policy-ai `
  --token-env GITHUB_TOKEN `
  --end 2026-11-27 `
  --output reports/canary-history-audit.json
```

## 不使用 Web 管理后台

当前版本没有 Web 管理后台。FastAPI 的根路径用于说明服务状态，主要部署探针是 `GET /health`；不要把它当作可视化控制台。日常操作通过 CLI、Scheduler 或消息机器人完成。

## 技术栈

| 层次 | 技术 |
|---|---|
| 运行时 | Python 3.11+ |
| Web/API | FastAPI、Uvicorn |
| 调度 | APScheduler |
| 数据访问 | SQLAlchemy、Alembic、SQLite/PostgreSQL |
| 采集 | Requests、BeautifulSoup、可扩展采集器结构、Scrapling、Playwright、Tavily |
| AI | OpenAI Python SDK，OpenAI 兼容 HTTP API |
| 消息 | 飞书机器人、企业微信智能机器人、企业微信自建应用、SMTP |
| 部署 | Docker、Docker Compose |

## 数据流

```text
政府网站 / 搜索服务
        │
        ▼
来源采集与发现（Requests / BeautifulSoup / Scrapling / Playwright / Tavily）
        │
        ▼
正文清洗、日期过滤、去重、重要性评分
        │
        ▼
SQLite 或 PostgreSQL（Policy / Analysis / AgentTask / NotificationRecord）
        │
        ▼
OpenAI 兼容模型结构化分析
        │
        ▼
日报 / 周报 / 月报 / 季报 Markdown
        │
        ├── 本地 reports/
        ├── SMTP 邮件
        ├── 飞书 / 企业微信群机器人
        └── 消息机器人对话与 FastAPI 回调
```

## 环境要求

- Python **3.11 或更高版本**；CI 固定使用 3.11。
- Windows 10/11、Linux 或 macOS。
- 可访问目标政府网站、AI API 和 Playwright 所需的浏览器依赖。
- Docker 部署需要 Docker Engine 或 Docker Desktop，以及 Compose v2。
- 首次安装 Playwright Chromium 需要网络和足够磁盘空间。

## 安装

推荐使用 `requirements.txt` 安装运行时依赖：

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
python -m pip check
```

如果 PowerShell 阻止激活脚本，可在当前用户范围调整执行策略，或将命令改为：

```powershell
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### Linux/macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install --with-deps chromium
python -m pip check
```

也可以在已安装依赖的环境中安装项目命令入口：

```powershell
python -m pip install -e .
pia --help
```

## 配置

从模板创建本地配置：

```powershell
Copy-Item .env.example .env
```

```bash
cp .env.example .env
```

至少需要检查：

| 变量 | 说明 | 默认/要求 |
|---|---|---|
| `AI_API_KEY` | AI 服务密钥 | 必须由使用者填写 |
| `AI_API_BASE` | OpenAI 兼容接口地址 | `https://api.deepseek.com` |
| `AI_MODEL` | 模型名称 | `deepseek-chat` |
| `DATABASE_URL` | 数据库连接串 | 本地默认 `sqlite:///data/policies.db` |
| `NOTIFY_CHANNELS` | 通知渠道开关，逗号分隔 | 默认为空，不发送通知 |
| `TAVILY_API_KEY` | 湖北、武汉政策发现的可选搜索增强 | 推荐配置；不配置时仍应通过直连、同源 JSON 或 Scrapling 回退，不是五来源运行硬依赖 |
| `SOURCE_HEALTH_ALERT_ENABLED` | 数据源连续失败告警 | 模板默认 `false` |

`.env` 已被 `.gitignore` 排除，禁止提交真实凭据。生产环境应使用平台密钥管理、容器 Secret 或宿主环境变量，而不是把值写入镜像或仓库。

### 政府来源配置

来源位于 `config.yaml` 的 `sources` 列表。新增来源时不修改业务代码，先复制已有条目并配置 `name`、`level`、`website`、`list_url`/`data_url`、`scraper_type`、`days_back` 和 `health_probe`。如果新站点需要新的页面结构，则还需要实现对应采集器并补上人工验收。

关键词、报告 TOP N 和输出目录也在 `config.yaml` 中维护。不要把账号密码、Cookie 或 Webhook 写入 `config.yaml`。

### 通知配置

`NOTIFY_CHANNELS` 是通知渠道的唯一开关：只有在其中列出的渠道才会被考虑，且对应凭据也必须完整。例如：

```env
NOTIFY_CHANNELS=email,wechat
SMTP_SERVER=smtp.qq.com
SMTP_PORT=465
SMTP_USER=your-account@example.com
SMTP_PASSWORD=your-smtp-app-password
NOTIFY_EMAIL=recipient@example.com
WECHAT_WEBHOOK_URL=https://example.invalid/replace-with-your-webhook
```

支持的通知标识为 `email`、`feishu`、`wechat`。不填写 `NOTIFY_CHANNELS` 时不会发送通知；如果渠道已启用但凭据缺失，系统会跳过并记录原因。不要把真实 Webhook 或密码提交到 Git。

## 数据库初始化与迁移

SQLite 首次启动不需要手工建表，以下命令会执行 Alembic 迁移：

```powershell
python main.py init
python -m alembic current
```

`python main.py init` 会区分空库、尚未接入 Alembic 的历史库和已接入迁移的数据库：空库执行初始迁移；结构完整的历史库补齐已知列并标记到当前 head；结构不完整时拒绝接管。不要用删除数据库的方式绕过迁移错误。

SQLite 默认文件为 `data/policies.db`，首次部署可先备份已有文件：

```powershell
New-Item -ItemType Directory -Force data | Out-Null
Copy-Item data\policies.db data\policies.db.bak-YYYYMMDD-HHMMSS
python main.py init
python -m alembic current
```

PostgreSQL 使用 `psycopg` 驱动，示例：

```text
DATABASE_URL=postgresql+psycopg://user:password@127.0.0.1:5432/policy_intelligence
```

真实密码应通过环境变量或 Secret 注入，不要写入 README、`.env.example` 或镜像。修改模型后应新增迁移：

```powershell
python -m alembic revision --autogenerate -m "describe_change"
python -m alembic upgrade head
python -m alembic check
```

迁移前停止写入并完成可恢复备份。SQLite 与 PostgreSQL 的迁移行为依赖实际版本、权限和已有数据，发布前应使用与生产一致的备份、恢复和回滚演练。

## 启动与使用

完成配置和数据库初始化后，先执行一次来源可用性验收：

```powershell
python main.py source-canary --json --output reports\source-canary.json
```

该命令逐个处理 `config.yaml` 中所有 `enabled: true` 的来源，真实获取列表并提取首条正文；默认要求正文至少 100 字符。退出码含义：`0` 为全部通过，`1` 为任一来源失败，`2` 为命令自身异常。可用 `--no-content` 只检查列表，或用 `--min-content-chars` 调整正文阈值。报告不含正文、请求头、凭据或带查询参数的私密地址。

注意事项：

- 结果只覆盖巡检时点，不能据此宣称外部政府网站永久可用。
- 选择器结构探针与运行链路 canary 是不同口径；固定选择器失败不代表降级链路不可用，以 canary 是否真实取得列表和正文为准。
- 人社部首条曾取得 227 字符正文；经人工核对，该文是简短的规章废止决定，227 字符是合法短文，不应为了通过阈值而降低验收标准或人为扩写。
- Tavily 可用于提高湖北、武汉的发现稳定性，但不是默认五来源运行的硬依赖；未配置时可经直连、同源 JSON 或 Scrapling 回退。

查看命令：

```powershell
python main.py --help
```

初始化并执行一次日报：

```powershell
python main.py init
python main.py daily --force
```

其他报告周期：

```powershell
python main.py weekly --force
python main.py monthly --force
python main.py quarterly --force
```

也可以只采集一个来源：

```powershell
python main.py daily --force --source 湖北省人民政府
python main.py sources
```

交互式 Agent：

```powershell
python -m app.agent.chat
```

启动 FastAPI（同时启动 Scheduler 和健康探针）：

```powershell
python -m uvicorn app.web:app --host 127.0.0.1 --port 8000
```

随后访问 `http://127.0.0.1:8000/health`。该接口返回数据库、Scheduler、最近任务和通知状态；生产环境应由反向代理提供 TLS、访问控制和限流。

### 企业微信智能机器人

智能机器人与企业微信群机器人 Webhook、自建应用是三套不同能力。智能机器人使用 `WECOM_BOT_ENABLED=true`、`WECOM_BOT_ID`、`WECOM_BOT_SECRET` 和白名单，通过 WebSocket 长连接运行，不占用 FastAPI 进程：

```powershell
python -m app.channels.wecom_bot
```

详细配置见 [`docs/wecom_bot.md`](docs/wecom_bot.md)。同一机器人只能保持一个有效长连接，因此不要多副本同时运行该进程。

## 通知与验收

默认模板不开启通知。建议先对已生成的报告做 dry-run：

```powershell
python main.py verify-notification --report reports\你的日报.md --channel email --json
```

加上 `--send` 才会真实发送。真实发送前应检查收件人、群机器人、报告内容和幂等任务 ID。当前 `--channel` 验收入口覆盖邮件和企业微信群机器人；飞书群机器人仍需按渠道配置单独做小规模人工验收。

## Docker Compose 部署

1. 准备 `.env` 和需要的运行数据目录。
2. 构建并启动：

```bash
docker compose up --build
```

3. 检查健康状态：

```bash
curl http://127.0.0.1:8000/health
docker compose ps
docker compose logs --tail=100 policy-agent
```

Compose 默认只启动 **FastAPI + Scheduler** 一个服务，并把 `data/`、`logs/`、`reports/` 挂载到宿主机。企业微信智能机器人是独立进程，不会被 Compose 自动启动；若要使用，需要单独运行 `python -m app.channels.wecom_bot`，并确保只有一个实例占用该机器人长连接。

容器构建会安装 Playwright 和 Chromium，首次构建耗时较长。国内网络可在构建参数中替换 Debian/PyPI 镜像；不要把带凭据的镜像源地址提交到仓库。停止服务：

```bash
docker compose down
```

`docker compose down -v` 会删除 Compose 管理的卷；如果生产数据已挂载到宿主机目录，仍需单独做备份确认。发布前应在干净机器上实际构建和启动，而不是只执行 `docker compose config`。

## 目录结构

```text
.
├── app/                    # 业务源码：采集、分析、Agent、报告、通知、Web
│   ├── agent/              # Agent、Scheduler、任务与会话
│   ├── analyzers/          # AI 结构化分析
│   ├── channels/           # 飞书、企业微信和消息事件
│   ├── data_layer/         # 检索、排序、来源校验
│   ├── models/             # SQLAlchemy 模型
│   ├── notifiers/          # 邮件、飞书、企业微信通知
│   ├── reporters/          # Markdown 报告渲染
│   ├── scrapers/           # 多来源采集与正文清洗
│   └── services/           # 去重、来源健康与诊断服务
├── alembic/                # 数据库迁移脚本
├── docs/                   # 使用与接入说明
├── .env.example            # 脱敏配置模板
├── config.yaml             # 来源和业务配置
├── main.py                 # CLI 入口
├── clean_garbled_data.py   # 历史数据乱码审计/修复工具
├── requirements.txt        # 运行时依赖
├── pyproject.toml          # 包元数据和命令入口
├── Dockerfile
├── docker-compose.yml
└── README.md
```

以下目录是运行产物，不应提交到 Git：`.env`、`data/`、`logs/`、`reports/`、虚拟环境、缓存、构建产物和本地临时文件。公开仓库只保留源码、配置模板、迁移和文档。

## 安全与合规

- 不提交 `.env`、数据库、日志、报告、Cookie、Token、密码、私钥、Webhook 和生产连接串。
- 采集前确认目标站点的 robots、服务条款、访问频率和版权要求；不得绕过验证码、访问控制或 WAF。
- 不要把政策分析结果冒充官方解释；报告中的结论应标注为 AI 分析，并保留原始链接供人工核验。
- 生产环境应限制 `/health` 和事件回调的暴露范围，启用 TLS、访问日志、速率限制和凭据轮换。
- 如果发现曾经的密钥或 Token 出现在历史提交中，删除文件不能替代撤销和轮换；应先停止公开，再处理历史。

## 常见问题

**`python main.py init` 报迁移错误**
先执行 `python -m alembic current`，确认数据库 URL、文件权限和已有表结构；停止写入并备份后排查，不要删除业务库重试。

**Playwright 启动失败**
确认执行了 `python -m playwright install chromium`；Linux 还要安装 `--with-deps` 所需的系统库。容器内需重新构建镜像。

**采集能运行但正文为空**
目标站点可能改版、触发 WAF、依赖 JavaScript 或需要额外权限。先运行 `python main.py source-canary --json` 检查实际运行链路，再运行 `python main.py source-health --json` 诊断固定 selector；不要用猜测的选择器代替真实验证。

**AI 调用失败或超时**
检查 `AI_API_BASE`、`AI_API_KEY`、`AI_MODEL` 和网络；缩短 `AI_REQUEST_TIMEOUT_SECONDS` 或降低 `ANALYZE_CONCURRENCY` 前先确认服务商限制。应用只信任成功取得的全文分析，失败不会伪造结果。

**没有收到通知**
确认 `NOTIFY_CHANNELS` 包含渠道，且对应凭据存在；默认不启用通知。使用 `python main.py notification-status --json` 查看状态，不要在生产环境随意重复发送。

**Docker 启动后服务不健康**
查看 `docker compose logs policy-agent`，确认 `.env` 已创建、数据库可写、`/app/data` 权限正确；首次构建失败不等于部署完成。

## 发布验收清单

- [x] 在全新 Python 3.12 虚拟环境执行 `pip install -r requirements.txt` 和 `pip check`。
- [x] 使用隔离 SQLite 执行 `python main.py init`、`python -m alembic current`。
- [x] 启动 FastAPI，确认 `/health` 返回 200 且数据库、Scheduler、Agent 状态正常，然后关闭测试进程。
- [x] 完成一次五来源真实采集和来源结构探针，并记录口径限制。
- [x] 补充 5 张来自真实数据的脱敏展示图、系统架构图和两项数据流图。
- [x] GitHub Actions Run `43430587587` 实际通过 PostgreSQL 16 初始化、迁移、读写往返和 API 健康检查。
- [x] GitHub Actions Run `43430587587` 实际执行 `docker compose up --build --detach` 并检查容器健康与 `/health`。
- [x] 使用真实 API 凭据完成一次受控的“采集 → AI 分析 → 报告生成 → 企业微信和邮件通知”链路；采集范围是全部 5 个启用来源，分析样本为 1 条。
- [ ] 在获得明确授权和测试渠道后，补充真实通知效果截图；Docker 部署截图不作为必需项。
- [x] 对全部 5 个启用来源连续执行两轮本地列表与首条正文的 `source-canary`，均为 5/5 通过。
- [x] GitHub Actions 手动 Canary Run `43430807699` / Artifact `14040291339` 完成 5/5 真实来源验收。
- [x] 每日 canary 采用固定 5 来源门禁，任一来源缺失、改名、列表为空或正文低于阈值时任务失败，并仅上传脱敏 Manifest。
- [ ] 从 2026-11-14 起完成至少 14 个北京时间自然日的 `event=schedule` 连续成功记录，并更新验收台账。
- [ ] 对最终发布提交与全部可达对象复扫，确认无 `.env`、数据库、日志、报告、测试文件、临时文件及敏感值。
- [ ] 删除远端 `codex/*` 临时分支，确认远端仅保留 `main` 且无标签。
- [ ] 通过无认证请求确认公开后仓库 `private: false`、README 和工作流可见。

## 许可证

本项目采用 [MIT License](LICENSE)。第三方网站内容、接口服务、模型输出及其许可仍归各自权利人，使用者需自行确认合规边界。
