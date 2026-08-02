# Personalized Research Daily

每天从 PubMed、bioRxiv、medRxiv 和 arXiv 获取新论文，按照手写研究兴趣排序，使用兼容 OpenAI API 的模型生成中文摘要，并通过邮件推送。Zotero 仍作为可选的兴趣来源保留，但默认不需要 Zotero。

## 当前功能

- PubMed 期刊白名单，以及 bioRxiv、medRxiv、arXiv 多来源检索。
- 按正向兴趣及权重排序，支持负向主题降权和最低相关分过滤。
- 按 PMID、DOI、URL、标题进行跨来源和跨天去重。
- 摘要语言、结构、提示词及长度均可在 YAML 中调整。
- 邮件展示来源、期刊、日期、DOI、PMID、相关分和匹配兴趣。
- 可随时将 `interest.provider` 改回 `zotero` 使用原 Zotero 资料库。

## GitHub Repository secrets

在仓库的 **Settings → Secrets and variables → Actions → Repository secrets** 中配置：

| Secret | 必需 | 说明 |
| --- | --- | --- |
| `NCBI_EMAIL` | 是 | NCBI E-utilities 要求的联系邮箱，不会显示在邮件正文中。 |
| `NCBI_API_KEY` | 否 | NCBI API key；不设置也能运行。 |
| `SENDER` | 是 | 发件邮箱。 |
| `SENDER_PASSWORD` | 是 | SMTP 授权码或密码。 |
| `RECEIVER` | 是 | 收件邮箱。 |
| `GEMINI_API_KEY` | 是 | Google AI Studio 创建的 Gemini API Key。 |

默认 SMTP 已配置为 Gmail：`smtp.gmail.com:587` + STARTTLS。`SENDER_PASSWORD` 必须填写 Google 账号生成的应用专用密码，而不是 Gmail 登录密码。

使用 Gmail 发信前：

1. 给 Google 账号开启两步验证。
2. 在 Google 账号的“应用专用密码”页面创建一个新密码，可命名为 `GitHub Literature Daily`。
3. 将生成的应用专用密码保存到 GitHub Secret `SENDER_PASSWORD`。
4. 将完整 Gmail 地址保存到 `SENDER`，例如 `name@gmail.com`。

不再需要 `ZOTERO_ID`、`ZOTERO_KEY` 或 Repository variable `CUSTOM_CONFIG`，除非主动切换回 Zotero 模式。

## 个性化配置

所有日常调整都在 [`config/custom.yaml`](config/custom.yaml) 中完成。

### 调整研究兴趣

```yaml
interest:
  provider: manual
  topics:
    - name: Protein and peptide science
      description: Protein engineering, peptide synthesis and biomolecular interactions.
      weight: 1.0
  negative_topics:
    - name: Unrelated case reports
      description: Single-patient reports without a broadly useful method or mechanism.
      weight: 1.0
  negative_penalty: 0.5
```

`weight` 越大，主题对最终排序的影响越大；`negative_penalty` 越大，命中排除主题时降分越明显。

### 调整摘要内容

修改 `llm.language`、`llm.summary.system_prompt` 和 `llm.summary.prompt_template`。模板支持：

- `{language}`
- `{title}`
- `{journal}`
- `{publication_date}`
- `{matched_topics}`
- `{abstract}`
- `{full_text}`

因此可以自由增删“方法、定量结果、创新点、局限、与研究兴趣的关系”等栏目。

### 调整来源和推送数量

```yaml
executor:
  source: [pubmed, biorxiv, medrxiv, arxiv]
  max_paper_num: 30
  min_score: 2.5
```

- 在 `source.pubmed.journals` 中增删期刊。
- 在相应的 `category` 中调整预印本分类。
- `min_score` 越高，邮件越精简。首次使用建议根据几天的结果再调整。
- `source.pubmed.lookback_days` 默认是 3 天，用于避免延迟收录；推送历史会阻止重复邮件。

PubMed 并不完整覆盖所有化学和材料期刊。当前列表适合作为起点，后续可增加 Crossref、Europe PMC 或出版商 RSS 来源。

## 运行

在 Actions 页面可以运行：

- **Check PubMed source**：只验证 PubMed 和 `NCBI_EMAIL`，不会调用 LLM，也不会发邮件。
- **Send emails daily**：完整执行；也会在每天 22:00 UTC 自动运行。

本地运行：

```bash
export NCBI_EMAIL=you@example.com
export SENDER=...
export RECEIVER=...
export SENDER_PASSWORD=...
export GEMINI_API_KEY=...
uv run src/zotero_arxiv_daily/main.py
```

推送历史默认写入 `data/sent_history.json`。GitHub Actions 使用 cache 在不同运行之间恢复该文件；本地运行时也会自动保留。

## 测试

```bash
uv run pytest
python -m compileall -q src scripts tests
```

项目基于 [TideDra/zotero-arxiv-daily](https://github.com/TideDra/zotero-arxiv-daily)，使用 AGPLv3 许可证。
