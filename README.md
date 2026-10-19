# AI Agent 论文每日摘要

每天从 arXiv 抓取 AI Agent 相关的新论文，用 Claude 打分、精读 Top 3，生成中文讲解，推送到飞书群机器人，并把当日结果存档为 Markdown。

流程：

```
arXiv API（最近 24h，cs.AI / cs.CL / cs.MA / cs.LG，只取元数据）
  → 关键词过滤（config.yaml）
  → Claude Haiku 分批打分 1–10 分（结构化 JSON）
  → 取 Top K，拉取 arxiv.org/html/<id> 抽取引言/方法/实验/结论（≤15000 字符）
  → Claude Sonnet 生成中文讲解（含 GitHub 链接）
  → 写入 digests/YYYY-MM-DD.md，并推送飞书卡片
```

## 目录结构

```
config.yaml            分类、关键词、精读篇数、模型名、token 预算、单价
src/
  fetch.py             arXiv 抓取与 Atom 解析
  filter.py            关键词过滤
  score.py             分批打分
  read.py              HTML 正文抽取与精读讲解
  notify.py            飞书卡片构造、签名、推送
  main.py              主流程与 Markdown 存档
  config.py            配置与环境变量读取
  usage.py             token 用量与成本统计
  models.py            数据结构
tests/                 pytest 单元测试（不联网）
digests/               每日存档，YYYY-MM-DD.md
.github/workflows/daily.yml   每日定时任务（北京时间 09:00）与手动触发
.env.example           环境变量示例
```

## 本地运行

需要 Python 3.11+。

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # 填入真实的密钥
set -a; source .env; set +a   # 把 .env 导入当前 shell

pytest                                      # 运行单元测试
python -m src.main --dry-run                # 完整流程，但不推送飞书
python -m src.main --max-papers 5 --dry-run # 只对前 5 篇候选打分（省钱调试）
python -m src.main                          # 完整运行并推送
```

`--date YYYY-MM-DD` 可以指定存档日期，默认为北京时间今天。运行日志会输出每次调用的输入/输出 token 数、单次成本，以及当日合计与预算使用率。

## 环境变量

所有密钥只从环境变量读取，不写入代码或仓库。

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | 是（有候选论文时） | Anthropic API Key |
| `ANTHROPIC_BASE_URL` | 否 | 覆盖 API 地址，默认 `https://api.anthropic.com` |
| `FEISHU_WEBHOOK_URL` | 是（非 dry-run） | 飞书自定义机器人 Webhook 地址 |
| `FEISHU_WEBHOOK_SECRET` | 否 | 机器人开启“签名校验”时填写 |
| `SCORE_MODEL` | 否 | 打分模型，默认 `claude-haiku-5-5`（见 config.yaml） |
| `READ_MODEL` | 否 | 精读模型，默认 `claude-sonnet-5-5` |
| `DAILY_TOKEN_BUDGET` | 否 | 单次运行的 token 预算（输入+输出），默认 300000 |

## 添加飞书机器人

1. 在飞书群里点击 **设置 → 群机器人 → 添加机器人 → 自定义机器人**。
2. 复制生成的 **Webhook 地址**，作为 `FEISHU_WEBHOOK_URL`。
3. 安全设置二选一（或都不开）：
   - **签名校验**：复制密钥作为 `FEISHU_WEBHOOK_SECRET`。
   - **自定义关键词**：添加关键词 `AI Agent`，卡片标题“AI Agent 论文日报”包含该词，可以通过校验。
   - 若使用 IP 白名单，注意 GitHub Actions 的出口 IP 不固定，不建议使用。

## GitHub Actions 配置

在仓库的 **Settings → Secrets and variables → Actions** 中添加：

- Secrets：`ANTHROPIC_API_KEY`、`FEISHU_WEBHOOK_URL`，可选 `FEISHU_WEBHOOK_SECRET`。
- Variables（可选）：`DAILY_TOKEN_BUDGET`、`SCORE_MODEL`、`READ_MODEL`。

工作流每天 UTC 01:00（北京时间 09:00）运行，也可在 **Actions → Daily AI Agent Paper Digest → Run workflow** 手动触发，支持 `max_papers` 与 `dry_run` 参数。运行结束后，存档会由 `github-actions[bot]` 提交到 `digests/`。工作流需要 `contents: write` 权限；如果默认分支开启了保护规则，需要允许该机器人推送，或改为提交到其他分支。

## 调整关键词与预算

编辑 `config.yaml`：

- **关键词**：`filter.keywords` 列表。匹配大小写不敏感，空格与连字符等价（`tool use` 同时匹配 `tool-use`），允许复数。`filter.min_hits` 设为 2 可以只保留同时命中两个关键词的论文。`filter.exclude_keywords` 用于排除误伤，例如 `reinforcement learning`。
- **精读篇数**：`reading.top_k`。
- **预算**：`budget.daily_tokens`，或设置环境变量 `DAILY_TOKEN_BUDGET`。超出预算后剩余论文跳过精读，只推送标题、评分和链接。
- **模型与截断长度**：`scoring.*`、`reading.*`。正文上限由 `reading.max_input_chars`（默认 15000 字符）控制。
- **单价**：`pricing_usd_per_mtok`，仅用于日志中的成本估算。请对照官方定价更新，默认值以发布时的文档为准。

## 成本控制

- 打分使用 `claude-haiku-5-5`，每批 10 篇，关闭 thinking，effort 为 `low`。
- 精读只针对 Top K 篇，正文按章节预算截断到 15000 字符以内。
- 每次调用的 `usage.input_tokens` / `usage.output_tokens` 都会被记录并换算成本。

### 每日 token 消耗的估算方法

设候选篇数为 N，每批 B 篇（默认 10），每篇摘要截断后约 300 token，系统提示约 400 token，精读 K 篇（默认 3）：

- 打分：`ceil(N / B) × (系统提示 + B × 每篇 token) + ceil(N / B) × 每批输出`，输出每篇约 30–60 token。
- 精读：`K × (系统提示 + 正文 ≤ 15000 字符 ÷ 约 3 字符/token + 输出上限 2000)`，正文约 4000–5000 token，单篇输入合计约 5000–6000 token。
- 合计：通常远低于默认预算 300000。例如 N = 100 时，打分约 3–5 万 token，精读约 2 万 token。

运行时的预算检查使用更保守的上界估算（输入按 1 token ≈ 3 字符，加输出上限）。运行日志中的实际数值以 API 返回的 usage 为准。

## 已知限制

- 部分论文没有 arXiv HTML 版本（404），会退回仅基于摘要的讲解，并在卡片中标注。
- `evaluation`、`planning` 等关键词较宽，候选数会偏多；可以通过 `min_hits` 或 `exclude_keywords` 收紧。
- 每日运行只有一次尝试，飞书推送失败会以非零码退出，由 Actions 报红，需要手动重跑。
- 批量打分采用同步分批调用。Message Batches API 成本约为一半，但是异步，可能需要等待数小时，对每日定时任务不够及时。
