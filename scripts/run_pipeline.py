#!/usr/bin/env python3
import os, re, json, argparse, requests
from pathlib import Path
from urllib.parse import quote
from html import unescape

LINKEDIN_QUERIES = [
    "art director photo video Los Angeles",
    "art director social media Los Angeles",
    "art director advertising Los Angeles",
    "content creator Los Angeles",
    "social media content creator Los Angeles",
    "creative director Los Angeles",
    "art director fashion Los Angeles",
    "creative assistant Los Angeles",
    "junior art director Los Angeles",
]


# LA metro area acceptable location strings (lowercase substrings)
LA_LOCATIONS = [
    "los angeles", "la,", "santa monica", "culver city", "el segundo",
    "manhattan beach", "venice", "west hollywood", "beverly hills",
    "burbank", "glendale", "pasadena", "torrance", "long beach",
    "inglewood", "compton", "hawthorne", "gardena", "carson",
    "calabasas", "woodland hills", "encino", "sherman oaks",
    "van nuys", "north hollywood", "studio city", "silver lake",
    "echo park", "koreatown", "downtown la", "dtla", "irvine",
    "orange county", "fullerton", "anaheim", "california", ", ca",
    "remote",  # accept remote jobs too
]

PLACEHOLDER_TOKENS = {"company", "position", "company name", "role"}

TARGET_ROLE_KEYWORDS = [
    "art director", "creative director", "content creator",
    "creative assistant", "visual director", "brand director",
    "creative strategist", "creative lead",
]


def load_env(env_path):
    for line in Path(env_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip().strip('"').strip("'")


def notion_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def ensure_note_column(db_id, h):
    db = requests.get(f"https://api.notion.com/v1/databases/{db_id}", headers=h, timeout=30).json()
    props = db.get("properties", {})
    if "note" in props and props["note"].get("type") == "rich_text":
        return
    requests.patch(
        f"https://api.notion.com/v1/databases/{db_id}",
        headers=h,
        data=json.dumps({"properties": {"note": {"rich_text": {}}}}),
        timeout=30,
    )


def query_all_rows(db_id, h):
    out, cur = [], None
    while True:
        payload = {"page_size": 100}
        if cur:
            payload["start_cursor"] = cur
        r = requests.post(
            f"https://api.notion.com/v1/databases/{db_id}/query",
            headers=h, data=json.dumps(payload), timeout=30
        ).json()
        out.extend(r.get("results", []))
        if not r.get("has_more"):
            break
        cur = r.get("next_cursor")
    return out


def fetch_existing_urls(db_id, h):
    """Return set of URLs already in the Notion database."""
    existing = set()
    cur = None
    while True:
        payload = {"page_size": 100}
        if cur:
            payload["start_cursor"] = cur
        r = requests.post(
            f"https://api.notion.com/v1/databases/{db_id}/query",
            headers=h, data=json.dumps(payload), timeout=30
        ).json()
        for page in r.get("results", []):
            url = page.get("properties", {}).get("URL", {}).get("url")
            if url:
                # Normalize: strip tracking params after ?
                existing.add(url.split("?")[0])
        if not r.get("has_more"):
            break
        cur = r.get("next_cursor")
    return existing


def title(prop):
    return "".join(x.get("plain_text", "") for x in prop.get("title", [])).strip()


def rich(prop):
    return "".join(x.get("plain_text", "") for x in prop.get("rich_text", [])).strip()


def is_placeholder(value):
    v = (value or "").strip().lower()
    return (not v) or any(tok == v or tok in v for tok in PLACEHOLDER_TOKENS)


def sanitize_filename(name):
    name = re.sub(r"[\\/:*?\"<>|]", "-", name)
    return re.sub(r"\s+", " ", name).strip()[:180]


def parse_job_cards(html):
    jobs = []
    for m in re.finditer(r"<li>(.*?)</li>", html, re.S):
        s = m.group(1)
        idm = re.search(r"jobPosting:(\d+)", s)
        hrefm = re.search(r'href="([^"]*linkedin\.com/jobs/view/[^"]+)"', s)
        titlem = re.search(r"<h3[^>]*>\s*(.*?)\s*</h3>", s, re.S)
        compm = re.search(r'job-search-card-subtitle"[^>]*>\s*(.*?)\s*</a>', s, re.S)
        if not (idm and hrefm and titlem and compm):
            continue
        # Extract location from card
        locm = re.search(r'job-search-card__location"[^>]*>\s*(.*?)\s*</span>', s, re.S)
        location = re.sub(r"<[^>]+>", "", unescape(locm.group(1))).strip() if locm else ""
        jobs.append({
            "id": idm.group(1),
            "url": unescape(hrefm.group(1)).replace("&amp;", "&"),
            "position": re.sub(r"<[^>]+>", "", unescape(titlem.group(1))).strip(),
            "company": re.sub(r"<[^>]+>", "", unescape(compm.group(1))).strip(),
            "location": location,
        })
    return jobs


def extract_jd_text(job_html):
    m = re.search(r'<div class="show-more-less-html__markup[^>]*>([\s\S]*?)</div>', job_html)
    if not m:
        return ""
    txt = m.group(1)
    txt = re.sub(r"<script[\s\S]*?</script>", "", txt)
    txt = re.sub(r"<style[\s\S]*?</style>", "", txt)
    txt = txt.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    txt = re.sub(r"</p>", "\n\n", txt)
    txt = re.sub(r"</li>", "\n", txt)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = unescape(txt)
    lines = [ln.strip() for ln in txt.splitlines()]
    noisy = []
    for ln in lines:
        l = ln.lower()
        if not ln:
            noisy.append("")
            continue
        if re.search(r"\b\d+[+,]?\s+applicants?\b", l):
            continue
        if re.search(r"\b(posted|reposted|\d+\s+(day|days|hour|hours|week|weeks)\s+ago)\b", l):
            continue
        if l in {"about the job", "job description"}:
            continue
        noisy.append(ln)
    txt = "\n".join(noisy)
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
    if len(txt) > 18000:
        txt = txt[:18000] + "\n\n[Truncated]"
    return txt


def jd_to_children(jd_text):
    if not jd_text.strip():
        return []
    chunks = []
    for para in [p.strip() for p in jd_text.split("\n\n") if p.strip()]:
        while len(para) > 1800:
            chunks.append(para[:1800])
            para = para[1800:]
        chunks.append(para)
    return [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": c}}]},
        }
        for c in chunks[:80]
    ]


def classify_priority(jd, position):
    """Return 'high' or 'low' based on photo/video/social vs graphic/brand design focus."""
    txt = (jd + "\n" + position).lower()

    high_signals = [
        "photo", "photograph", "video", "shoot", "on-set", "on set",
        "production", "campaign shoot", "editorial", "social media",
        "content creation", "tiktok", "instagram", "reels", "youtube",
        "lifestyle shoot", "commercial shoot", "art direct", "art direction",
        "stills", "cinematograph", "director of photography", "visual storytell",
    ]
    low_signals = [
        "graphic design", "graphic designer", "logo design", "typography",
        "layout design", "print design", "packaging design", "identity design",
        "design system", "brand guidelines", "brand identity", "ui design",
        "ux design", "web design", "infographic", "motion graphic",
        "branding agency", "brand design", "visual identity",
    ]

    high_count = sum(1 for s in high_signals if s in txt)
    low_count = sum(1 for s in low_signals if s in txt)

    # Low priority if graphic/brand signals dominate and no strong photo/video signals
    if low_count >= 3 and high_count <= 1:
        return "low"
    if low_count > high_count and low_count >= 2:
        return "low"
    return "high"




def extract_hourly_rate(txt):
    """Return the lowest hourly rate found in the text, or None if not found."""
    # Match patterns like $15/hr, $15.50/hour, $15 per hour, $15-$20/hr
    patterns = [
        r"\$\s*(\d+(?:\.\d+)?)\s*[-–]\s*\$?\s*\d+(?:\.\d+)?\s*(?:/\s*hr|per\s+hour|/hour)",
        r"\$\s*(\d+(?:\.\d+)?)\s*(?:/\s*hr|per\s+hour|/hour|an\s+hour)",
    ]
    for pat in patterns:
        m = re.search(pat, txt, re.I)
        if m:
            return float(m.group(1))
    return None


def reject_reason(jd, position, location=""):
    txt = (jd + "\n" + position).lower()
    loc_lower = location.lower()

    # --- Location check ---
    # If card location is set and not in LA metro, reject immediately
    if location and not any(la in loc_lower for la in LA_LOCATIONS):
        return "wrong_location"

    # Also scan JD for strong non-US location signals
    non_us_signals = [
        "croatia", "zagreb", "sydney", "melbourne", "london", "toronto",
        "dubai", "singapore", "hong kong", "amsterdam", "berlin",
    ]
    if any(sig in txt for sig in non_us_signals):
        return "wrong_location"

    # --- Role check ---
    if not any(kw in position.lower() for kw in TARGET_ROLE_KEYWORDS):
        return "non_target_role"

    # --- Short gig check (one-day events, day-of shoots, etc.) ---
    if re.search(r"\bone[\s-]day\b|\b1[\s-]day\s+(event|shoot|job|gig)\b|day of (event|show|shoot)", txt):
        return "short_gig"

    # --- Part-time pay check: must be >= $20/hr ---
    is_part_time = bool(re.search(r"\bpart[\s-]time\b|\bpart time\b", txt))
    if is_part_time:
        hourly = extract_hourly_rate(txt)
        if hourly is not None and hourly < 20:
            return "low_pay_parttime"
        # If part-time with no stated pay, accept (can't verify, let user judge)

    # --- Exclusion filters ---
    if any(x in txt for x in ["must be a u.s. citizen", "us citizen only", "security clearance required"]):
        return "citizenship_only"
    if "phd" in txt and ("required" in txt or "must have" in txt):
        return "phd_only"

    return None


def run(args):
    load_env(args.env)
    token = os.environ["NOTION_TOKEN"]
    db_id = os.environ["NOTION_DATABASE_ID"]
    h = notion_headers(token)
    ensure_note_column(db_id, h)

    summary = {
        "searched": 0, "accepted": 0, "rejected": 0,
        "rejected_by_reason": {
            "duplicate": 0, "wrong_location": 0, "non_target_role": 0,
            "short_gig": 0, "low_pay_parttime": 0,
            "citizenship_only": 0, "phd_only": 0, "other": 0,
        },
        "added": 0, "high_priority": 0, "low_priority": 0,
        "resumes_generated": 0, "status_updated": 0,
        "failures": [],
    }

    existing_urls = fetch_existing_urls(db_id, h)
    seen = set()
    accepted_jobs = []

    # --- LinkedIn (paginated, 48h window) ---
    for q in LINKEDIN_QUERIES:
        if len(accepted_jobs) >= args.max_accept:
            break
        for start in [0, 10, 20]:
            if len(accepted_jobs) >= args.max_accept:
                break
            url = (
                f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
                f"?keywords={quote(q)}&location=Los+Angeles+Metropolitan+Area"
                f"&f_TPR=r172800&sortBy=DD&start={start}"
            )
            try:
                html = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25).text
            except Exception as e:
                summary["failures"].append({"source": "linkedin", "query": q, "error": str(e)})
                break
            cards = parse_job_cards(html)
            if not cards:
                break  # no more results for this query
            summary["searched"] += len(cards)
            for j in cards:
                if j["id"] in seen:
                    continue
                seen.add(j["id"])
                try:
                    jd_html = requests.get(
                        f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{j['id']}",
                        headers={"User-Agent": "Mozilla/5.0"}, timeout=25,
                    ).text
                except Exception as e:
                    summary["failures"].append({"job_id": j["id"], "error": str(e)})
                    continue
                if j["url"].split("?")[0] in existing_urls:
                    summary["rejected"] += 1
                    summary["rejected_by_reason"]["duplicate"] += 1
                    continue
                reason = reject_reason(jd_html, j["position"], j.get("location", ""))
                if reason:
                    summary["rejected"] += 1
                    summary["rejected_by_reason"][reason] = summary["rejected_by_reason"].get(reason, 0) + 1
                    continue
                j["jd_text"] = extract_jd_text(jd_html)
                j["priority"] = classify_priority(jd_html, j["position"])
                j["source"] = "linkedin"
                accepted_jobs.append(j)
                summary["accepted"] += 1
                if len(accepted_jobs) >= args.max_accept:
                    break
            if len(accepted_jobs) >= args.max_accept:
                break
        if len(accepted_jobs) >= args.max_accept:
            break

    for j in accepted_jobs:
        priority = j.get("priority", "high")
        note_parts = [f"priority: {priority}", f"src: {j.get('source', 'linkedin')}"]
        if j.get("location"):
            note_parts.append(f"loc: {j['location']}")
        if priority == "low":
            note_parts.append("graphic/brand design heavy — review before applying")
        payload = {
            "parent": {"database_id": db_id},
            "properties": {
                "Name": {"title": [{"type": "text", "text": {"content": j["company"][:200]}}]},
                "URL": {"url": j["url"]},
                "Position": {"rich_text": [{"type": "text", "text": {"content": j["position"][:2000]}}]},
                "Status": {"select": {"name": "Not started"}},
                "note": {"rich_text": [{"type": "text", "text": {"content": " | ".join(note_parts)}}]},
            },
            "children": jd_to_children(j.get("jd_text", "")),
        }
        r = requests.post(
            "https://api.notion.com/v1/pages", headers=h,
            data=json.dumps(payload), timeout=30
        ).json()
        if r.get("object") == "page":
            summary["added"] += 1
            summary[f"{priority}_priority"] += 1
        else:
            summary["failures"].append({
                "company": j["company"], "position": j["position"],
                "error": r.get("message", "unknown"),
            })

    summary["script_scope"] = "linkedin_to_notion_jd_only"
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="./.secrets/notion.env")
    ap.add_argument("--output-dir", default="./generated_resumes")
    ap.add_argument("--max-accept", type=int, default=10)
    run(ap.parse_args())
