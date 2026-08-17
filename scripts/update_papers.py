from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "papers.json"
METRICS = ROOT / "data" / "journal_metrics.json"
BERLIN = ZoneInfo("Europe/Berlin")

# 搜索尽量覆盖你的 3 条主线，但避免把“泛电化学”论文全部抓进来。
TOPICS = {
    "organic": [
        "electrocarboxylation carbon dioxide",
        "electrochemical carboxylation CO2",
        "reductive carboxylation CO2 electrochemistry",
        "nickel electrocarboxylation CO2",
        "diene electrocarboxylation",
        "organic electrosynthesis CO2 carboxylation",
    ],
    "gde": [
        "gas diffusion electrode CO2 electrolysis flooding wetting",
        "gas diffusion layer CO2 electrolyzer tomography",
        "micro CT gas diffusion electrode electrolysis",
        "X-ray tomography porous electrode catalyst layer",
        "3D reconstruction micro CT porous electrode",
    ],
    "analysis": [
        "cyclic voltammetry EC mechanism kinetics",
        "cyclic voltammetry coupled chemical reaction",
        "nonaqueous reference electrode ferrocene electrochemistry",
        "reference electrode DMF electrochemistry",
        "electrochemical impedance spectroscopy porous electrode method",
        "rotating ring disk electrode electrochemical mechanism",
    ],
}

# 分值从 0 开始，而不是原来的 55。只有真正命中核心词才会进入推荐。
WEIGHTS = {
    "organic": {
        "electrocarboxylation": 34,
        "electrochemical carboxylation": 30,
        "reductive carboxylation": 28,
        "carboxylation": 16,
        "carbon dioxide": 12,
        "co2": 12,
        "electrosynthesis": 10,
        "nickel": 9,
        "ni-catal": 8,
        "diene": 8,
        "alkene": 6,
        "aryl": 5,
        "dmf": 5,
    },
    "gde": {
        "gas diffusion electrode": 28,
        "gas diffusion layer": 25,
        "gde": 20,
        "micro-ct": 24,
        "micro ct": 24,
        "microcomputed tomography": 24,
        "x-ray tomography": 22,
        "x ray tomography": 22,
        "tomography": 14,
        "3d reconstruction": 15,
        "flooding": 14,
        "wetting": 12,
        "catalyst layer": 10,
        "porous electrode": 9,
        "co2 electrolysis": 12,
        "co2 electroly": 10,
    },
    "analysis": {
        "cyclic voltammetry": 22,
        "voltammetric": 14,
        "ec mechanism": 22,
        "ece mechanism": 22,
        "coupled chemical reaction": 18,
        "kinetic analysis": 14,
        "reaction mechanism": 10,
        "reference electrode": 22,
        "ferrocene": 15,
        "fc/fc+": 15,
        "nonaqueous": 10,
        "non-aqueous": 10,
        "dmf": 8,
        "electrochemical impedance spectroscopy": 18,
        "impedance spectroscopy": 14,
        "eis": 10,
        "rotating ring-disk": 18,
        "rotating ring disk": 18,
        "rrde": 16,
        "tafel": 10,
        "transfer coefficient": 14,
    },
}

# 这些词经常带来与你当前研究无关的“泛电化学应用”论文。
NEGATIVE_TERMS = {
    "direct methanol fuel cell": 45,
    "dmfc": 45,
    "pem fuel cell": 40,
    "proton exchange membrane fuel cell": 40,
    "oxygen reduction reaction": 28,
    "orr": 22,
    "oxygen evolution reaction": 28,
    "oer": 22,
    "supercapacitor": 35,
    "lithium-ion battery": 35,
    "lithium ion battery": 35,
    "sodium-ion battery": 35,
    "sodium ion battery": 35,
    "zinc-ion battery": 35,
    "water splitting": 28,
    "hydrogen evolution reaction": 24,
    "her catalyst": 24,
    "photocatal": 20,
}

# 每天不要塞太多。网页更像“精选简报”而不是搜索结果页。
MAX_FRESH = 9
MAX_PER_TOPIC = 3
MIN_SCORE = 72


def reconstruct_abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    words = []
    for word, positions in inv.items():
        for pos in positions:
            words.append((pos, word))
    return " ".join(word for _, word in sorted(words))


def normalize(text: str) -> str:
    text = text.lower().replace("₂", "2")
    text = re.sub(r"[\u2010-\u2015]", "-", text)
    text = re.sub(r"\s+", " ", text)
    return f" {text.strip()} "


def contains_any(text: str, terms: list[str] | tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def hard_gate(topic: str, title: str, abstract: str) -> bool:
    """先做资格审查，避免靠堆通用关键词把无关论文推高。"""
    title_l = normalize(title)
    full = normalize(title + " " + abstract)

    if topic == "organic":
        carbox = contains_any(
            full,
            (
                "electrocarboxylation",
                "electrochemical carboxylation",
                "reductive carboxylation",
                "carboxylation",
            ),
        )
        co2 = contains_any(full, (" co2 ", "carbon dioxide"))
        electro = contains_any(full, ("electro", "cathod", "electrosynthesis"))
        return carbox and co2 and electro

    if topic == "gde":
        core = contains_any(
            full,
            (
                "gas diffusion electrode",
                "gas diffusion layer",
                " gde ",
                "micro-ct",
                "micro ct",
                "microcomputed tomography",
                "x-ray tomography",
                "x ray tomography",
                "tomography",
            ),
        )
        context = contains_any(
            full,
            (
                "co2",
                "electroly",
                "electrode",
                "catalyst layer",
                "porous",
                "flooding",
                "wetting",
                "reconstruction",
                "segmentation",
            ),
        )
        return core and context

    if topic == "analysis":
        method = contains_any(
            full,
            (
                "cyclic voltammetry",
                "voltammetric",
                "reference electrode",
                "ferrocene",
                "fc/fc+",
                "electrochemical impedance spectroscopy",
                "impedance spectroscopy",
                "rotating ring-disk",
                "rotating ring disk",
                "rrde",
                "transfer coefficient",
            ),
        )
        mechanistic = contains_any(
            full,
            (
                "mechanism",
                "kinetic",
                "coupled chemical reaction",
                "reference electrode",
                "calibration",
                "nonaqueous",
                "non-aqueous",
                "porous electrode",
                "equivalent circuit",
                "mass transport",
            ),
        )

        # 如果标题本身明确是燃料电池/电池/超级电容等应用论文，除非标题也明确是方法学研究，否则直接排除。
        title_negative = contains_any(
            title_l,
            (
                "fuel cell",
                "dmfc",
                "battery",
                "supercapacitor",
                "water splitting",
                "oxygen reduction",
                "oxygen evolution",
            ),
        )
        title_method = contains_any(
            title_l,
            (
                "voltammetry",
                "mechanism",
                "kinetic",
                "reference electrode",
                "impedance spectroscopy",
                "method",
                "calibration",
            ),
        )
        if title_negative and not title_method:
            return False
        return method and mechanistic

    return False


def score_paper(topic: str, title: str, abstract: str) -> tuple[int, list[str]]:
    title_l = normalize(title)
    full = normalize(title + " " + abstract)
    score = 0
    matched: list[str] = []

    for term, weight in WEIGHTS[topic].items():
        if term in full:
            score += weight
            matched.append(term)
            # 标题命中比只在摘要中出现更重要。
            if term in title_l:
                score += max(3, round(weight * 0.35))

    # Review / perspective 对“雷达”很有价值，但不应压过主题相关性。
    if contains_any(title_l, ("review", "perspective", "tutorial")):
        score += 5

    for term, penalty in NEGATIVE_TERMS.items():
        if term in full:
            score -= penalty

    return max(0, min(99, score)), matched


def read_minutes(abstract: str) -> int:
    if not abstract:
        return 8
    words = len(abstract.split())
    return max(6, min(18, round(words / 180) + 4))


def fallback_reason(topic: str, matched: list[str]) -> str:
    top = "、".join(matched[:4]) if matched else "核心关键词"
    if topic == "organic":
        return f"命中 {top}，与有机电羧化、CO₂ 引入及 Ni/非水体系研究直接相关，优先检查底物、电极与反应条件。"
    if topic == "gde":
        return f"命中 {top}，与 GDE 结构、润湿/淹没行为或 Micro-CT 三维表征相关，可用于结构—性能关联分析。"
    return f"命中 {top}，属于电化学方法/机理分析类文献，可用于 CV、EIS、参比校准或动力学数据解释。"


def fallback_summary(topic: str, title: str, abstract: str) -> str:
    # 不假装是 AI 翻译；无 API 时给出清楚的“主题型摘要”。
    if topic == "organic":
        prefix = "该研究围绕有机电化学 CO₂ 羧化/还原羧化展开。"
    elif topic == "gde":
        prefix = "该研究聚焦 GDE、孔结构或 X-ray/Micro-CT 三维表征。"
    else:
        prefix = "该研究聚焦电化学测试方法、反应机理或动力学分析。"
    if abstract:
        return prefix + " 当前为规则筛选摘要；打开原文可查看完整实验设计与结论。"
    return prefix + f" 题目：{title}"


def fetch(topic: str, query: str, date_from: str, date_to: str) -> list[dict]:
    params = {
        "search": query,
        "filter": f"from_publication_date:{date_from},to_publication_date:{date_to},is_paratext:false",
        "sort": "publication_date:desc",
        "per-page": 40,
    }
    mail = os.getenv("OPENALEX_MAILTO")
    if mail:
        params["mailto"] = mail

    response = requests.get("https://api.openalex.org/works", params=params, timeout=30)
    response.raise_for_status()

    out: list[dict] = []
    for work in response.json().get("results", []):
        title = (work.get("title") or "").strip()
        if not title:
            continue
        abst = reconstruct_abstract(work.get("abstract_inverted_index"))

        if not hard_gate(topic, title, abst):
            continue

        score, matched = score_paper(topic, title, abst)
        if score < MIN_SCORE:
            continue

        location = work.get("primary_location") or {}
        source = (location.get("source") or {}).get("display_name") or "Unknown journal"
        doi = work.get("doi")
        url = doi or location.get("landing_page_url") or work.get("id")

        out.append(
            {
                "id": work.get("id", "").split("/")[-1]
                or re.sub(r"\W+", "-", title.lower())[:60],
                "topic": topic,
                "badge": "最新研究",
                "read_minutes": read_minutes(abst),
                "title": title,
                "journal": source,
                "year": work.get("publication_year"),
                "summary_zh": "",
                "abstract": abst[:3000],
                "relevance_reason": "",
                "tags": matched[:4],
                "score": score,
                "url": url,
                "archive": False,
            }
        )
    return out


def enrich_llm(papers: list[dict]) -> list[dict]:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return papers

    from openai import OpenAI

    client = OpenAI(api_key=key)
    model = os.getenv("OPENAI_MODEL", "gpt-5.6")

    for paper in sorted(papers, key=lambda x: x["score"], reverse=True):
        prompt = f"""你是电化学文献雷达。仅根据给出的标题与摘要生成严格 JSON，不要补充摘要中没有的信息。
字段：
summary_zh：70-120 字中文摘要，说明研究对象、方法和核心结论；
relevance_reason：40-80 字，具体说明它为什么与 Ni/DMF/CO2 electrocarboxylation、GDE/Micro-CT 或电化学分析方法相关；
tags：3-4 个简短标签。

标题：{paper['title']}
摘要：{paper.get('abstract', '')[:6000]}
"""
        try:
            resp = client.responses.create(model=model, input=prompt)
            txt = resp.output_text.strip()
            txt = re.sub(r"^```json|```$", "", txt).strip()
            obj = json.loads(txt)
            paper["summary_zh"] = obj.get("summary_zh", "")
            paper["relevance_reason"] = obj.get("relevance_reason", "")
            paper["tags"] = obj.get("tags", paper.get("tags", []))[:4]
        except Exception as exc:
            print("LLM enrich failed:", exc, file=sys.stderr)
        time.sleep(0.2)
    return papers


def add_fallback_text(paper: dict) -> None:
    if not paper.get("summary_zh"):
        paper["summary_zh"] = fallback_summary(
            paper["topic"], paper["title"], paper.get("abstract", "")
        )
    if not paper.get("relevance_reason"):
        paper["relevance_reason"] = fallback_reason(
            paper["topic"], paper.get("tags", [])
        )
    if not paper.get("tags"):
        paper["tags"] = [paper["topic"], "latest", "screened"]


def select_balanced(candidates: list[dict]) -> list[dict]:
    """每个主题最多 3 篇，避免某个宽泛主题占满首页。"""
    selected: list[dict] = []
    counts = {topic: 0 for topic in TOPICS}

    for paper in sorted(candidates, key=lambda x: x["score"], reverse=True):
        topic = paper["topic"]
        if counts[topic] >= MAX_PER_TOPIC:
            continue
        selected.append(paper)
        counts[topic] += 1
        if len(selected) >= MAX_FRESH:
            break

    return selected


def main() -> None:
    now = datetime.now(BERLIN)

    old = (
        json.loads(DATA.read_text(encoding="utf-8"))
        if DATA.exists()
        else {"papers": [], "trackers": []}
    )
    metrics = (
        json.loads(METRICS.read_text(encoding="utf-8")) if METRICS.exists() else {}
    )

    for paper in old.get("papers", []):
        paper["archive"] = True

    # 用 21 天窗口避免某一天数据库索引稍晚导致漏文献；去重后只展示精选项。
    start = (now.date() - timedelta(days=21)).isoformat()
    end = now.date().isoformat()

    found: list[dict] = []
    for topic, queries in TOPICS.items():
        for query in queries:
            try:
                found.extend(fetch(topic, query, start, end))
            except Exception as exc:
                print("fetch failed", topic, query, exc, file=sys.stderr)

    # OpenAlex 同一篇文章可能被多个 query 命中；保留相关度最高的版本。
    dedup: dict[str, dict] = {}
    for paper in sorted(found, key=lambda x: x["score"], reverse=True):
        key = paper.get("url") or paper["title"].lower()
        if key not in dedup:
            dedup[key] = paper

    fresh = select_balanced(list(dedup.values()))
    enrich_llm(fresh)

    for paper in fresh:
        add_fallback_text(paper)
        metric = metrics.get(paper["journal"], {})
        paper["impact_factor"] = metric.get("impact_factor")
        paper["quartile"] = metric.get("quartile", "Q —")

    fresh_ids = {paper["id"] for paper in fresh}
    archive = [
        paper
        for paper in old.get("papers", [])
        if paper.get("id") not in fresh_ids
    ][:180]

    high_count = sum(1 for paper in fresh if paper["score"] >= 88)
    match_score = round(
        sum(p["score"] for p in fresh) / len(fresh)
    ) if fresh else 0

    payload = {
        "brief_date": now.strftime("%Y · %m · %d"),
        "last_updated": now.date().isoformat(),
        "match_score": match_score,
        "trackers": old.get("trackers", []),
        "papers": fresh + archive,
    }

    DATA.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"Updated {len(fresh)} fresh papers ({high_count} high relevance), "
        f"{len(archive)} archived."
    )


if __name__ == "__main__":
    main()
