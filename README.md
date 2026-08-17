# e⁻CarbonScope — standalone literature radar

这是 `ElectroCatalysis Literature Radar` 的独立静态版。网页、GitHub Pages 和 GitHub Actions 不使用 ChatGPT Work/Codex 配额。

## 功能
- 今日文献 / 历史推送 / 主题筛选 / 搜索
- 相关度评分
- 已读 / 删除（localStorage）
- 工作日 Berlin 时间约 08:30 自动抓取 OpenAlex 新论文
- 可选：用 OpenAI API 生成中文摘要与“为什么与你相关”——API 与 ChatGPT 订阅分开计费，不消耗 Work credits
- JIF / JCR 分区来自 `data/journal_metrics.json` 手动维护；脚本不会伪造未知期刊指标

## 最省事的部署：GitHub Pages
1. 新建一个 GitHub repository，把本文件夹全部上传到仓库根目录。
2. Repository → Settings → Pages → Build and deployment → Deploy from a branch。
3. Branch 选 `main`，Folder 选 `/ (root)`，保存。
4. Repository → Actions，首次允许 workflow 运行。
5. 如果只想完全免费：不设置任何 OpenAI secret；页面仍会每日更新论文，但中文摘要会显示模板提示。
6. 如果希望和原网页一样自动生成中文摘要：Repository → Settings → Secrets and variables → Actions → New repository secret，添加 `OPENAI_API_KEY`。可选再添加 `OPENALEX_MAILTO`。

## 本地预览
不能直接双击 `index.html`（浏览器会阻止本地 fetch JSON）。在目录中运行：

```bash
python -m http.server 8000
```
然后打开 `http://localhost:8000`。

## 关于 08:30
GitHub Actions 的 cron 使用 UTC，且不理解欧洲夏令时，所以 workflow 同时安排 06:30 UTC 和 07:30 UTC；Python 脚本会检查 `Europe/Berlin` 当前小时，只让正确的一次执行更新。GitHub 的 schedule 可能有几分钟延迟。

## 期刊 IF / 分区
JCR 数据不是 OpenAlex 的开放字段，因此采用 `data/journal_metrics.json` 白名单。遇到新期刊时页面会显示 `IF — · Q —`，直到你手动补入指标，避免错误数据。
