from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "papers.json"
METRICS = ROOT / "data" / "journal_metrics.json"
BERLIN = ZoneInfo("Europe/Berlin")

# Three research lines. Queries are deliberately broader than the final filter:
# retrieval should be permissive; ranking/filtering decides what reaches the page.
TOPICS = {
    "organic": [
        "electrocarboxylation CO2",
        "electrochemical carboxylation carbon dioxide",
        "reductive carboxylation CO2 electrochemistry",
        "electrochemical carbon dioxide fixation organic",
        "nickel electrosynthesis CO2 carboxylation",
        "diene electrocarboxylation",
    ],
    "gde": [
        "gas diffusion electrode CO2 electrolysis",
        "gas diffusion layer CO2 electrolyzer",
        "GDE flooding wetting CO2",
        "X-ray tomography gas diffusion electrode",
        "micro CT porous electrode catalyst layer",
        "3D reconstruction tomography porous electrode",
    ],
    "analysis": [
        "cyclic voltammetry reaction mechanism kinetics",
        "cyclic voltammetry coupled chemical reaction",
        "nonaqueous reference electrode ferrocene",
        "reference electrode DMF electrochemistry",
        "electrochemical impedance spectroscopy porous electrode",
        "rotating ring disk electrode mechanism",
    ],
}

WEIGHTS = {
    "organic": {
        "electrocarboxylation": 36,
        "electrochemical carboxylation": 34,
        "reductive carboxylation": 31,
        "carboxylation": 18,
        "carbon dioxide": 11,
        " co2 ": 11,
        "carbon dioxide fixation": 18,
        "co2 fixation": 18,
        "electrosynthesis": 10,
        "electroreduction": 7,
        "nickel": 8,
        "diene": 8,
        "alkene": 5,
        "aryl": 4,
        "dmf": 5,
    },
    "gde": {
        "gas diffusion electrode": 30,
        "gas diffusion layer": 28,
        " gde ": 24,
        " gdl ": 22,
        "micro-ct": 27,
        "micro ct": 27,
        "microcomputed tomography": 27,
        "x-ray tomography": 25,
        "x ray tomography": 25,
        "tomography": 14,
        "3d reconstruction": 16,
        "segmentation": 11,
        "flooding": 15,
        "wetting": 12,
        "catalyst layer": 10,
        "porous electrode": 10,
        "co2 electrolysis": 13,
        "co2 electroly": 11,
    },
    "analysis": {
        "cyclic voltammetry": 22,
        "voltammetric": 13,
        "ec mechanism": 23,
        "ece mechanism": 23,
        "coupled chemical reaction": 19,
        "kinetic": 8,
        "mechanism": 8,
        "reference electrode": 24,
        "ferrocene": 16,
        "fc/fc+": 16,
        "nonaqueous": 12,
        "non-aqueous": 12,
        "dmf": 7,
        "electrochemical impedance spectroscopy": 20,
        "impedance spectroscopy": 15,
        "equivalent circuit": 10,
        "rotating ring-disk": 20,
        "rotating ring disk": 20,
        "rrde": 18,
        "tafel": 9,
        "transfer coefficient": 15,
        "mass transport": 7,
    },
}

# Strong penalties for common false-positive application areas.
NEGATIVE_TERMS = {
    "direct methanol fuel cell": 50,
    "dmfc": 50,
    "pem fuel cell": 45,
    "proton exchange membrane fuel cell": 45,
    "supercapacitor": 40,
    "lithium-ion battery": 40,
    "lithium ion battery": 40,
    "sodium-ion battery": 40,
    "sodium ion battery": 40,
    "zinc-ion battery": 40,
    "photocatal": 24,
    "water splitting": 20,
    "oxygen reduction reaction": 18,
    "oxygen evolution reaction": 18,
}

# Thresholds are topic-specific. The previous global threshold (72) was too strict
# for GDE/Micro-CT and electrochemical-method papers, which often contain fewer of
# our exact keywords even when they are highly useful.
MIN_RAW_SCORE = {
    "organic": 38,
    "gde": 32,
    "analysis": 34,
}

MAX_FRESH = 9
MAX_PER_TOPIC = 3
MIN_DAILY = 3
PRIMARY_DAYS = 35
FALLBACK_DAYS = 120


def reconstruct_abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    words: list[tuple[int, str]] = []
    for word, positions in inv.items():
        for pos in positions:
            words.append((pos, word))
    return " ".join(word for _, word in sorted(words))


def normalize(text: str) -> str:
    text = (text or "").lower().replace("₂", "2")
    text = re.sub(r"[\u2010-\u2015]", "-", text)
    text = re.sub(r"\s+", " ", text)
    return f" {text.strip()} "


def contains_any(text: str, terms: tuple[str, ...] | list[str]) -> bool:
    return any(term in text for term in terms)


def hard_gate(topic: str, title: str, abstract: str) -> bool:
    """Reject obvious false positives without demanding too many exact phrases."""
    title_l = normalize(title)
    full = normalize(title + " " + abstract)

    # Fuel-cell/battery papers are the most common false positives in the method topic.
    strong_negative_title = contains_any(
        title_l,
        (
            "direct methanol fuel cell",
            " dmfc ",
            "pem fuel cell",
            "battery",
            "supercapacitor",
        ),
    )

    if topic == "organic":
        # Exact electrocarboxylation is sufficient by itself.
        if "electrocarboxylation" in full:
            return True
        carbox = contains_any(
            full,
            (
                "electrochemical carboxylation",
                "reductive carboxylation",
                " carboxylation ",
                "carbon dioxide fixation",
                "co2 fixation",
            ),
        )
        co2 = contains_any(full, (" co2 ", "carbon dioxide"))
        electro = contains_any(full, ("electrochem", "electrosynth", "electroreduc", "cathod"))
        return carbox and co2 and electro

    if topic == "gde":
        gde_core = contains_any(
            full,
            ("gas diffusion electrode", "gas diffusion layer", " gde ", " gdl "),
        )
        tomography_core = contains_any(
            full,
            (
                "micro-ct",
                "micro ct",
                "microcomputed tomography",
                "x-ray tomography",
                "x ray tomography",
                "tomography",
            ),
        )
        electro_context = contains_any(
            full,
            (
                "co2",
                "electroly",
                "electrode",
                "catalyst layer",
                "porous",
                "flooding",
                "wetting",
            ),
        )
        imaging_context = contains_any(
            full,
            (
                "electrode",
                "catalyst layer",
                "porous",
                "segmentation",
                "reconstruction",
                "microstructure",
            ),
        )
        return (gde_core and electro_context) or (tomography_core and imaging_context)

    if topic == "analysis":
        if strong_negative_title:
            return False
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
        context = contains_any(
            full,
            (
                "mechanism",
                "kinetic",
                "coupled chemical reaction",
                "calibration",
                "nonaqueous",
                "non-aqueous",
                "porous electrode",
                "equivalent circuit",
                "mass transport",
                "reference electrode",
                "ferrocene",
            ),
        )
        return method and context

    return False


def raw_score(topic: str, title: str, abstract: str) -> tuple[int, list[str]]:
    title_l = normalize(title)
    full = normalize(title + " " + abstract)
    score = 0
    matched: list[str] = []

    for term, weight in WEIGHTS[topic].items():
        if term in full:
            score += weight
            matched.append(term.strip())
            if term in title_l:
                score += max(3, round(weight * 0.40))

    if contains_any(title_l, ("review", "perspective", "tutorial", "protocol")):
        score += 5

    for term, penalty in NEGATIVE_TERMS.items():
        if term in full:
            score -= penalty

    return max(0, score), matched


def display_score(topic: str, raw: int) -> int:
    """Map accepted raw scores to the 72-99 range used by the UI."""
    threshold = MIN_RAW_SCORE[topic]
    # 72 at threshold; increasingly strong matches approach 99.
    return min(99, 72 + max(0, round((raw - threshold) * 0.7)))


def read_minutes(abstract: str) -> int:
    if not abstract:
        return 8
    words = len(abstract.split())
    return max(6, min(18, round(words / 180) + 4))


def fallback_reason(topic: str, matched: list[str]) -> str:
    top = "、".join(matched[:4]) if matched else "核心关键词"
    if topic == "organic":
        return f"命中 {top}，与有机电羧化、CO₂ 引入及 Ni/非水体系研究直接相关，优先检查底物、电极和反应条件。"
    if topic == "gde":
        return f"命中 {top}，与 GDE 结构、润湿/淹没行为或 Micro-CT 三维表征相关，可用于结构—性能关联分析。"
    return f"命中 {top}，属于电化学方法/机理分析文献，可用于 CV、EIS、参比校准或动力学数据解释。"


def fallback_summary(topic: str, title: str, abstract: str) -> str:
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
        "per-page": 50,
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
        abstract = reconstruct_abstract(work.get("abstract_inverted_index"))

        if not hard_gate(topic, title, abstract):
            continue

        rank, matched = raw_score(topic, title, abstract)
        if rank < MIN_RAW_SCORE[topic]:
            continue

        location = work.get("primary_location") or {}
        source = (location.get("source") or {}).get("display_name") or "Unknown journal"
        doi = work.get("doi")
        url = doi or location.get("landing_page_url") or work.get("id")
        pub_date = work.get("publication_date") or ""

        out.append(
            {
                "id": work.get("id", "").split("/")[-1]
                or re.sub(r"\W+", "-", title.lower())[:60],
                "topic": topic,
                "badge": "最新研究",
                "read_minutes": read_minutes(abstract),
                "title": title,
                "journal": source,
                "year": work.get("publication_year"),
                "publication_date": pub_date,
                "summary_zh": "",
                "abstract": abstract[:3000],
                "relevance_reason": "",
                "tags": matched[:4],
                "score": display_score(topic, rank),
                "_rank_score": rank,
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


def deduplicate(papers: list[dict]) -> list[dict]:
    dedup: dict[str, dict] = {}
    for paper in sorted(papers, key=lambda x: x.get("_rank_score", 0), reverse=True):
        key = (paper.get("url") or paper["title"]).lower()
        if key not in dedup:
            dedup[key] = paper
    return list(dedup.values())


def select_balanced(candidates: list[dict], limit: int = MAX_FRESH) -> list[dict]:
    selected: list[dict] = []
    counts = {topic: 0 for topic in TOPICS}

    # Round-robin by topic first, then fill remaining slots by global rank.
    grouped = {
        topic: sorted(
            [p for p in candidates if p["topic"] == topic],
            key=lambda x: (x.get("_rank_score", 0), x.get("publication_date", "")),
            reverse=True,
        )
        for topic in TOPICS
    }

    for _ in range(MAX_PER_TOPIC):
        for topic in TOPICS:
            if grouped[topic] and counts[topic] < MAX_PER_TOPIC and len(selected) < limit:
                paper = grouped[topic].pop(0)
                selected.append(paper)
                counts[topic] += 1

    if len(selected) < limit:
        chosen = {p["id"] for p in selected}
        rest = sorted(
            [p for p in candidates if p["id"] not in chosen],
            key=lambda x: (x.get("_rank_score", 0), x.get("publication_date", "")),
            reverse=True,
        )
        selected.extend(rest[: limit - len(selected)])

    return selected[:limit]


def mark_recency_badge(paper: dict, now: datetime) -> None:
    pub = paper.get("publication_date")
    if not pub:
        return
    try:
        age = (now.date() - date.fromisoformat(pub)).days
    except ValueError:
        return
    paper["badge"] = "最新研究" if age <= PRIMARY_DAYS else "近期精选"


def collect_window(now: datetime, days: int) -> list[dict]:
    start = (now.date() - timedelta(days=days)).isoformat()
    end = now.date().isoformat()
    found: list[dict] = []
    for topic, queries in TOPICS.items():
        for query in queries:
            try:
                found.extend(fetch(topic, query, start, end))
            except Exception as exc:
                print("fetch failed", topic, query, exc, file=sys.stderr)
    return deduplicate(found)


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

    # First search recent literature. If that yields too few strong matches, expand
    # the publication window rather than showing an empty daily brief.
    candidates = collect_window(now, PRIMARY_DAYS)
    fresh = select_balanced(candidates)

    if len(fresh) < MIN_DAILY:
        extended = collect_window(now, FALLBACK_DAYS)
        fresh = select_balanced(extended)

    enrich_llm(fresh)

    for paper in fresh:
        add_fallback_text(paper)
        mark_recency_badge(paper, now)
        metric = metrics.get(paper["journal"], {})
        paper["impact_factor"] = metric.get("impact_factor")
        paper["quartile"] = metric.get("quartile", "Q —")
        paper.pop("_rank_score", None)

    fresh_ids = {paper["id"] for paper in fresh}
    archive = [
        paper
        for paper in old.get("papers", [])
        if paper.get("id") not in fresh_ids
    ][:180]

    high_count = sum(1 for paper in fresh if paper["score"] >= 88)
    match_score = round(sum(p["score"] for p in fresh) / len(fresh)) if fresh else 0

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
        f"Updated {len(fresh)} recommended papers ({high_count} high relevance), "
        f"{len(archive)} archived."
    )


if __name__ == "__main__":
    main()
