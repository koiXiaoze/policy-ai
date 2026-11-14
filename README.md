# Policy Intelligence Agent

> 本地优先的政府政策采集、结构化分析与趋势报告工具。

## 项目简介

Policy Intelligence Agent 面向个人政策研究与信息整理场景：从政府网站采集政策文件，清洗正文，调用兼容 OpenAI 接口的模型进行结构化分析，生成日报、周报、月报和季报，并通过可选渠道发送通知。

项目采用“官方来源优先、搜索补充发现、官网读取正文”的分层策略：

- 国务院继续使用稳定的官方 JSON，不消耗第三方搜索额度；
- 湖北、武汉、十堰、人社部等存在 WAF、JavaScript 渲染或连接波动的来源，可使用百度 AI 搜索补充政策发现；
- 百度结果与官网/Scrapling 结果合并去重，百度返回空或少量结果时不会短路官网通道；
- 搜索摘要不作为政策全文，正文仍从政府官网读取；Tavily 仅保留为官网正文提取失败后的兜底。

> 项目定位是信息整理与分析工具，不构成投资、就业、法律或政务决策建议。政策原文、数据来源与模型输出都应人工复核。

### 搜索增强原则

1. **权威正文只来自政府官网。** 搜索 API 返回的标题、日期、URL 和摘要只用于发现候选政策。
2. **官方结构化接口单轨运行。** 国务院 `ZUIXINZHENGCE.json` 已验证稳定，不调用百度搜索。
3. **搜索不是唯一真相源。** 实测百度对湖北、武汉部分政策详情页存在索引缺口，因此搜索必须与官网、Scrapling 或同源 JSON 合并。
4. **失败语义必须可验证。** 搜索空结果只表示“本次未发现可验证候选”，不能直接推断官网无更新。

## 功能概览

- 采集国家级、省级和市级政策来源，来源通过 `config.yaml` 配置；默认包含国务院、湖北省、武汉市、十堰市、人社部。
- 使用 Requests、BeautifulSoup、Scrapling 和 Playwright 处理常规页面与需要浏览器渲染的页面；百度 AI 搜索负责补充政策发现，搜索结果与官网结果合并去重，不承担正文权威。
- 使用 SQLAlchemy + Alembic 管理 SQLite/PostgreSQL 数据；当前表结构覆盖政策、分析、任务、通知、会话和数据源健康状态。
- 使用 OpenAI 兼容 API 生成结构化 JSON 分析，兼容 OpenAI、DeepSeek、通义千问等服务。
- 生成日报、周报、月报、季报 Markdown 文件；支持交互式 CLI 与企业微信智能机器人对话。
- 通过邮件、飞书群机器人、企业微信群机器人发送报告；通知默认关闭，必须显式配置。
- 提供 FastAPI `/health` 探针、Scheduler、飞书/企业微信事件回调和数据源健康诊断能力。

### 效果与实际验证

以下素材来自 2026-11-12 至 2026-11-14 的脱敏验证：真实 AI 分析记录、来源运行链路巡检、来源结构探针、百度 AI 搜索接入验收，以及企业微信、邮件实发台账。它们用于说明当前效果，**不代表全量历史、持续稳定性或生产 SLA**。SQLite、Alembic 和 FastAPI `/health` 已在本地隔离环境实测；最终提交的 PostgreSQL 16、Docker Compose 与健康检查由 GitHub Actions 复验。政府站点 Canary 继续作为发布后巡检和诊断门禁，但外部站点的 WAF、区域网络或第三方搜索配额可能使托管 Runner 失败；该结果不等同于软件部署链路失效，也不构成永久可用承诺。企业微信和 QQ 邮箱通知已获取真实效果图；飞书未配置且未实发。

#### 真实 AI 分析样例

![真实 AI 分析样例：电力安全事故应急处置和调查处理条例](docs/assets/ai-analysis-sample.png)

样例记录：`policy_id=9`、`analysis_id=8`、模型 `auto`、全文 6,740 字符、耗时 27.31 秒、趋势评分 88；分析状态为成功并标记为可信结果。AI 输出仅供参考，须结合原文人工复核。

#### 真实日报内容预览

![真实日报内容预览：一句话总结、政策大白话、产业影响与大学生建议](docs/assets/daily-report-preview.png)

此图使用上述真实落库分析内容整理，不展示数据库路径、URL、账号或 Token。它是内容形态预览，不能替代生产环境通知投递截图。

#### 一次五来源实际采集量

![五个来源本次实际采集量：合计 69 条](docs/assets/source-yield.png)

一次真实 `fetch()` 调用中，五个来源均返回 `ok`，合计取得 69 条；其中国务院 29、湖北省 20、武汉市 7、十堰市 5、人社部 8。该图是采集量样本；`source-canary` 另行验证每个启用来源的列表与首条正文。单次成功不等于持续稳定，也不代表逐条页面正文都已验证。

2026-11-14 接入百度搜索后的本地完整巡检再次取得 5/5 通过：国务院 29 条、湖北 20 条、武汉 4 条、十堰 5 条、人社部 8 条，五个来源首条正文均达到验收阈值。该次实测同时发现百度对湖北、武汉部分官网政策详情页存在索引缺口，最终数据主要由官网/Scrapling 通道取得，百度结果按候选发现补充并合并。该结论只对应本次测试时点。

#### 来源结构健康诊断

![五个来源结构健康诊断：正常 2，WAF 拦截 2，未配置 selector 1](docs/assets/source-health.png)

结构探针结果为正常 2/5、WAF 拦截 2/5、未配置 selector 1/5。**“实际调用返回 ok”与“结构健康”是两个不同口径**：本次没有绕过 WAF，也未把异常结构统计为采集成功。结构探针用于诊断固定选择器和页面形态；`source-canary` 则验证包含同源 JSON、浏览器和搜索辅助在内的实际运行链路，两套结果不能互相替代。

#### 趋势评分样本

![七条已保存分析的匿名趋势评分样本](docs/assets/trend-sample.png)

展示 7 条已保存分析的趋势评分，平均 83.4、中位数 82。旧记录中的部分标题存在编码异常，因此只展示匿名样本编号，不将乱码标题或内容当作有效展示材料。该图是样本分布，不是全量政策趋势图。

### 系统架构

```mermaid
flowchart LR
    A[国务院官方 JSON] --> D[来源发现与候选合并]
    B[政府网站列表 / 同源 JSON] --> D
    C[百度 AI 搜索：仅发现] --> D
    D --> E[官网正文抓取与清洗]
    E --> F[SQLite / PostgreSQL]
    F --> G[OpenAI 兼容模型]
    G --> F
    F --> H[Markdown 报告]
    H --> I[本地 reports/]
    H --> J[邮件 / 飞书 / 企业微信通知]
    F --> K[FastAPI /health]
    S[APScheduler] --> D
    S --> G
    S --> H
    F --> L[消息机器人与事件回调]
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

#### 真实通知效果

以下图片来自 2026-11-13 的企业微信与 QQ 邮箱真实通知发送结果。截图仅展示脱敏后的报告内容，不包含群成员、手机号、邮箱地址、Webhook、企业 ID、Token 或内部链接。

![企业微信真实通知效果：政策趋势日报](docs/assets/wecom-sample.png)

*企业微信：真实收到“政策趋势日报 - 国务院”，含报告日期、来源、评分、一句话总结及政策影响分析。*

![QQ 邮箱真实通知效果：政策报告正文](docs/assets/email-sample.png)

*QQ 邮箱：真实收到包含政策影响、机会与建议的报告正文。该截图用于证明通知内容能够送达；它不展示发件人或收件人信息。*

Docker 部署截图不作为项目介绍页的展示项。

## 当前验证状态与发布边界

以下状态记录于 **2026-11-14**，用于区分“已经证明什么”和“尚未证明什么”。未执行的项目不会被写成通过。“长期可用”在本项目中定义为：`config.yaml` 中全部 5 个启用来源每日自动巡检，真实取得非空列表并验证首条正文；任一来源失败、缺失或名称变化，任务必须失败。

| 验证项 | 当前状态 | 证据或剩余条件 |
|---|---|---|
| 全新 Python 环境安装 | 本地已通过，最终提交待复验 | 在 Python 3.12.12 的隔离虚拟环境中执行 `pip install -r requirements.txt` 与 `pip check`；最终提交还将由 GitHub Actions 使用全新 Python 3.11 复验依赖安装、`pip check`、SQLite 初始化和健康检查。 |
| SQLite 初始化与迁移 | 已通过 | 使用隔离 SQLite 执行 `python main.py init`，Alembic 版本为 `91a4d2f7c8b0 (head)`。 |
| FastAPI 健康检查 | 已通过 | Uvicorn 实际启动后，`GET /health` 返回 HTTP 200；数据库、Scheduler 和 Agent 状态正常。 |
| Release Smoke Check | 由最终提交复验 | `.github/workflows/ci.yml` 在全新 Ubuntu 与 Python 3.11 中执行依赖安装、SQLite 初始化和 `/health`；PostgreSQL 16 job 执行迁移、真实读写和 API 健康检查；Docker job 实际执行 `docker compose up --build --detach`，并检查容器 `healthy` 与 `/health`。发布结论以最终 `main` 提交的最新成功运行结果为准，不在文档中固化运行编号。 |
| 当前启用来源 Canary | 本地最终代码 5/5 | 2026-11-14 在百度 AI 搜索接入后的本地最终代码逐源执行 Canary，5/5 通过：国务院 29 条/正文 12000 字符、湖北省 20/5216、武汉市 4/7027、十堰市 5/2851、人社部 8/227。Canary 是来源可用性诊断，不作为软件版本永久 SLA 或发布日期前置条件；GitHub 托管 Runner 的历史失败见下方诊断记录。 |
| 百度 AI 搜索接入 | 已通过真实 Key 本地受控验收 | 千帆 AI 搜索标准版真实请求返回 HTTP 200 和 `request_id`，鉴权、额度错误分类、日期过滤、站点过滤及政策门禁均通过动态验证。实测湖北、武汉部分官网政策详情页未被百度索引，因此实现始终保留官网/Scrapling 通道并与搜索候选合并；本次验收约消耗 21 次搜索调用。CI 不使用真实百度凭据。 |
| AI 分析 | 已通过（历史受控样本） | 在隔离 SQLite 环境中真实采集全部 5 个启用来源并取得 69 条；受控选取 1 条送入流水线，由兼容模型完成全文结构化分析，趋势评分 72，生成日报并落库。该结果只证明一次受控样本链路；CI 不会使用真实模型凭据。 |
| 真实通知投递 | 企业微信、邮件历史受控实发；飞书未验证 | 2026-11-13 实际发送企业微信 2/2 分片、邮件 1/1 分片，均无重试，台账状态为 `success`。该结果为历史受控样本，CI 不会发送真实通知；飞书未配置且未实发，不能记为通过。 |
| 来源巡检自动化 | 已配置，托管结果待新提交复验 | `.github/workflows/source-canary.yml` 每日 00:17 UTC（北京时间 08:17）运行，使用固定 5 来源名称、数量、列表数和正文阈值门禁；仅上传脱敏 Manifest。仓库需配置同名 `BAIDU_API_KEY` Secret；未配置时仍回退官网、Scrapling 和本地数据。每日巡检用于发现和诊断外部站点变化，不承诺永久可用。 |
| 发布后滚动趋势 | 已配置，尚未积累 | 仅统计每日 `event=schedule` 的 Canary；用于发布后观察近期可用性，不阻断发布日期，也不替代发布时的最终 HEAD 验证。失败或缺失会记录并触发排查，不会预填未来通过记录。连续 14 天验证不作为本次发布的前置条件。 |
| 敏感信息审计 | 最终提交前重跑 | 公开发布前必须对最终发布提交和全部 `origin` Ref 执行阻断式全历史审计，并确认无高危凭据格式或实际非空配置值。审计未通过时不得切换为 Public。 |
| 可公开性 | 发布时核验 | 仓库为单根仓库，跟踪树不得包含测试、数据库、日志、报告或缓存路径。最终 `main` 提交的 Release Smoke Check 和全历史安全审计通过后，切换为 Public 并完成匿名核验；政府站点托管 Canary 的结果需如实保留，不以伪造或重试筛选替代。 |

发布后滚动记录见 [`docs/source-canary-acceptance.md`](docs/source-canary-acceptance.md)。滚动趋势只作为发布后的质量观察；若定时 Canary 出现真实失败，应修复并更新部署，而不是把观察窗口未满当作阻止正式版本发布的理由。

### 发布路径结论

发布使用当前 GitHub 单根仓库，不需要重建发布快照。公开发布顺序为：

1. 完成文档、配置模板、工作流和敏感信息清理，并提交到 `main`。
2. 等待最终发布提交的 Release Smoke Check 全部成功，并核验 PostgreSQL 16、Docker Compose 与 `/health` 的真实执行结果。
3. 对最终发布提交和全部 `origin` Ref 执行全历史安全审计；同时确认远端只保留预期分支，且不存在临时标签。
4. 记录最近一次本地与托管环境 Government Source Canary 的完整结果；失败必须如实保留，不阻断仅由外部站点访问限制导致的软件版本发布。
5. 在旧 GitHub 凭据已撤销或轮换的前提下，将仓库切换为 Public。
6. 通过无认证请求确认 `private: false`、README 和工作流文件可访问，并保留每日 Canary 和滚动趋势巡检。

全历史安全审计或 Release Smoke Check 失败时必须暂停公开；滚动趋势未满不阻塞发布。外部政府网站改版、WAF 策略变化或区域网络中断无法被代码永久消除，长期可用性通过持续巡检、失败告警和及时修复维持。

### 政府站点 Canary 诊断记录

2026-11-13 的历史结果对应当前已被百度方案替代的 Tavily 基线。两套环境使用不同出口网络与第三方配额，不能互相替代：

| 环境 | 运行 | 结果 | 说明 |
|---|---|---|---|
| 本地最终代码 | 手动全来源 Canary | 5/5 通过 | 五个来源均取得非空列表和首条正文；正文长度见验证状态表。 |
| GitHub 托管 Runner | `43447139528` | 1/5 通过 | 国务院通过；湖北正文为空；武汉 Tavily 配额耗尽；十堰三条通道均连接失败；人社部触发 WAF。 |
| GitHub 托管 Runner | `43448420186` | 1/5 通过 | 国务院通过；湖北、武汉为 `quota_exhausted`；十堰直连、Tavily、Scrapling 三通道失败；人社部为 `waf_challenge`。Artifact `14048933270` 为脱敏 Manifest。 |

这两次托管失败发生在相同提交和相同核心采集代码上，失败来源均涉及外部访问、WAF 或第三方配额，而非 Python 依赖安装、SQLite 初始化、Playwright 安装或工作流执行错误。工作流门禁仍会如实失败，不能把 Artifact 解释为 5/5 通过。部署使用者应根据本地网络条件自行运行 `source-canary`，并结合日志排查具体来源。

2026-11-14 百度 AI 搜索接入后的本地复验结果：

| 环境 | 运行 | 结果 | 说明 |
|---|---|---|---|
| 本地最终代码 | 手动全来源 Canary | 5/5 通过 | 百度对湖北、武汉站点过滤查询均返回 0 条有效政策；官网/Scrapling 随后分别取得 20 条和 4 条。最终条目数为国务院 29、湖北 20、武汉 4、十堰 5、人社部 8，首条正文均通过阈值。 |

该结果证明的是“百度搜索叠加官方回退后的当前本地运行链路”，不证明百度索引已经完整覆盖湖北、武汉政策详情页，也不代表 GitHub 托管 Runner 已达到 5/5。

### 发布审计命令

如需查看发布后的定时 Canary 趋势，可运行以下命令。该报告用于质量观察，不构成发布截止日期：

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

Canary 趋势脚本的退出码为：`0` 表示窗口完整通过；`1` 表示证据不足或近期巡检失败；`2` 表示 API、格式、网络或报告写入异常。它在 Release Safety Audit 工作流中只生成报告，不阻断发布。`audit_public_release.py` 是唯一阻断公开发布的历史安全审计；其退出码为：`0` 表示通过；`1` 表示安全门禁失败；`2` 表示 API、格式、网络或报告写入异常。每日 01:17 UTC（北京时间 09:17）运行趋势与安全审计；空窗口或部分窗口不会被推测为“未来已通过”。

## 不使用 Web 管理后台

当前版本没有 Web 管理后台。FastAPI 的根路径用于说明服务状态，主要部署探针是 `GET /health`；不要把它当作可视化控制台。日常操作通过 CLI、Scheduler 或消息机器人完成。

## 技术栈

| 层次 | 技术 |
|---|---|
| 运行时 | Python 3.11+ |
| Web/API | FastAPI、Uvicorn |
| 调度 | APScheduler |
| 数据访问 | SQLAlchemy、Alembic、SQLite/PostgreSQL |
| 采集 | Requests、BeautifulSoup、可扩展采集器结构、Scrapling、Playwright、百度 AI 搜索、Tavily 正文兜底 |
| AI | OpenAI Python SDK，OpenAI 兼容 HTTP API |
| 消息 | 飞书机器人、企业微信智能机器人、企业微信自建应用、SMTP |
| 部署 | Docker、Docker Compose |

## 数据流

```text
政府网站 / 搜索服务
        │
        ▼
来源采集与发现（Requests / BeautifulSoup / Scrapling / Playwright / 百度 AI 搜索 / Tavily 正文兜底）
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

## 配置说明

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
| `BAIDU_API_KEY` | 湖北、武汉、十堰、人社部政策发现 | 推荐配置；国务院来源明确不消耗百度额度 |
| `TAVILY_API_KEY` | 官网正文读取失败后的提取兜底 | 可选；百度搜索摘要不作为政策全文 |
| `SOURCE_HEALTH_ALERT_ENABLED` | 数据源连续失败告警 | 模板默认 `false` |

`.env` 已被 `.gitignore` 排除，禁止提交真实凭据。生产环境应使用平台密钥管理、容器 Secret 或宿主环境变量，而不是把值写入镜像或仓库。

### 百度 AI 搜索额度

项目使用千帆 AI 搜索标准版，按搜索调用次数计费，不按 `top_k` 条数计费。按百度智能云当前公开口径，标准版每月提供 1500 次免费调用，按天发放约每天 50 次，标准版超出后的公开单价为 0.036 元/次；控制台扣费方式和实时额度以账户当时页面为准。不要把这组数字写成程序内的永久硬编码。来源过滤属于限时免费的增强能力，不应视为永久免费条款。

默认配置下，湖北和武汉每天各有 2 次优先搜索，共 4 次；十堰和人社部仅在官网通道失败时各追加最多 2 次。即使两个回退来源每天都触发，单次日采集约 8 次，约 240 次/月，占月免费额度的 16%。GitHub 来源 Canary、人工重跑和失败重试会额外消耗额度，因此必须结合控制台的调用统计做实际核算。

不要默认认为免费额度耗尽后一定不会产生费用。上线前应在百度智能云控制台确认是否已开通后付费、余额提醒和配额告警；若要求绝对零扣费，应确保未开通会产生扣费的结算方式，并保留失败降级路径。

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
- 百度 AI 搜索用于提高湖北、武汉、十堰、人社部等 WAF/JS 来源的发现稳定性；国务院继续只使用官方结构化来源，不消耗百度额度。
- 实测百度对湖北、武汉部分官网政策详情页存在索引缺口，因此百度返回空或少量结果时仍会执行官网/Scrapling，并与搜索结果合并去重；百度不是湖北、武汉的独占数据源。
- Tavily 不再承担政策发现，只在官网正文读取失败时作为提取兜底。

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

## 发布核验流程

执行发布时逐项确认并保留证据，不用历史成功代替最终提交验证：

1. 在全新 Python 环境执行 `pip install -r requirements.txt`、`pip check` 和 `python main.py init`。
2. 运行 PostgreSQL 16 迁移、真实读写往返和 API `/health` 检查。
3. 实际执行 `docker compose up --build --detach`，检查容器健康状态和 `/health`，再执行 `docker compose down`。
4. 使用真实 AI、通知凭据的验证仅在获得授权并确认测试渠道后进行；不要在 CI 日志或截图中暴露凭据、收件人、群成员或 Webhook。
5. 运行固定五来源 Canary，记录列表数、正文状态和脱敏 Artifact；外部失败如实记录，不能改写为通过。
6. 对最终提交与全部可达 Git 对象执行阻断式安全审计，确认无 `.env`、数据库、日志、报告、测试文件、临时文件及敏感值。
7. 确认 `origin` 仅保留 `main` 且无临时标签；发布内容验收通过后，为最终提交创建带注释的正式版本标签和 GitHub Release。
8. 通过无认证请求确认 `private: false`、README、工作流和通知效果图可访问；飞书需在具备授权渠道后单独验证，Docker 部署截图不作为介绍页必需项。

## 许可证

本项目采用 [MIT License](LICENSE)。第三方网站内容、接口服务、模型输出及其许可仍归各自权利人，使用者需自行确认合规边界。

## 版本与发布

当前代码版本为 `0.1.0`，也是本仓库的首个公开发布基线。版本变更与验证范围见 [CHANGELOG.md](CHANGELOG.md)。GitHub Release 应始终指向已通过 Release Smoke Check 和全历史安全审计的最终提交；历史标签不代表当前部署状态。
