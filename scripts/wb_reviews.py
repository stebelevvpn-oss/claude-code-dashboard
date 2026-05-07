"""Download all reviews for a Wildberries product by article ID.

Usage:
    python wb_reviews.py <article_id> [--out FILE]

Output: JSON file with all reviews, ratings, and aggregates.
Default output path: Desktop\\wb_<id>_reviews.json
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def fetch_json(url, timeout=15):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Accept-Encoding": "identity",
            "Origin": "https://www.wildberries.ru",
            "Referer": "https://www.wildberries.ru/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        if r.headers.get("Content-Encoding") in ("gzip", "x-gzip"):
            import gzip
            body = gzip.decompress(body)
        return json.loads(body.decode("utf-8", errors="replace"))


def extract_article(s: str) -> int:
    s = s.strip()
    if s.isdigit():
        return int(s)
    m = re.search(r"/catalog/(\d+)/", s)
    if m:
        return int(m.group(1))
    raise ValueError(f"Cannot extract article from: {s}")


def find_basket(nm: int, vol: int, part: int) -> int | None:
    """Locate which basket-XX shard hosts this product (same trick as wb_download.py)."""
    for i in range(1, 32):
        host = f"basket-{i:02d}.wbbasket.ru"
        url = f"https://{host}/vol{vol}/part{part}/{nm}/info/ru/card.json"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=8) as r:
                if r.status == 200:
                    return i
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                continue
        except Exception:
            continue
    return None


def get_card(nm: int) -> dict:
    vol = nm // 100000
    part = nm // 1000
    b = find_basket(nm, vol, part)
    if b is None:
        raise RuntimeError(f"Cannot find basket for nm={nm}")
    host = f"basket-{b:02d}.wbbasket.ru"
    url = f"https://{host}/vol{vol}/part{part}/{nm}/info/ru/card.json"
    return fetch_json(url)


def get_imt_id(card: dict) -> int:
    imt = card.get("imt_id") or card.get("imtId")
    if imt:
        return int(imt)
    raise RuntimeError("imt_id not in card.json")


def get_product_name(card: dict) -> str:
    parts = [card.get("imt_name") or card.get("imt_name_long") or "", card.get("subj_name") or ""]
    return " · ".join(p for p in parts if p)


def fetch_feedbacks(imt: int) -> dict:
    """Try feedbacks1/feedbacks2 shards, prefer v2 (slightly larger window)."""
    last_err = None
    for shard in ("feedbacks1.wb.ru", "feedbacks2.wb.ru"):
        for version in ("v2", "v1"):
            url = f"https://{shard}/feedbacks/{version}/{imt}"
            try:
                data = fetch_json(url, timeout=30)
                if data and (data.get("feedbacks") is not None):
                    return data
            except urllib.error.HTTPError as e:
                last_err = f"{shard}/{version}: HTTP {e.code}"
                continue
            except Exception as e:
                last_err = f"{shard}/{version}: {e}"
                continue
    raise RuntimeError(f"No reviews returned (last: {last_err})")


def nm_distribution_from_response(raw: dict, nm_id: int) -> dict | None:
    """Extract per-nm rating distribution from the API response, if available."""
    block = raw.get("nmValuationDistribution")
    if isinstance(block, dict):
        if block.get("nm") == nm_id:
            return block.get("valuationDistribution") or None
    if isinstance(block, list):
        for item in block:
            if item.get("nm") == nm_id:
                return item.get("valuationDistribution") or None
    return None


def summarize(feedbacks: list) -> dict:
    by_rating = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    photos = 0
    videos = 0
    with_text = 0
    for f in feedbacks:
        r = f.get("productValuation") or f.get("valuation") or 0
        if r in by_rating:
            by_rating[r] += 1
        if f.get("photo") or f.get("photos"):
            photos += 1
        if f.get("video") or f.get("videos"):
            videos += 1
        if (f.get("text") or "").strip() or (f.get("pros") or "").strip() or (f.get("cons") or "").strip():
            with_text += 1
    total = len(feedbacks)
    avg = sum(k * v for k, v in by_rating.items()) / max(1, sum(by_rating.values()))
    return {
        "total": total,
        "with_text": with_text,
        "with_photo": photos,
        "with_video": videos,
        "rating_avg": round(avg, 2),
        "rating_distribution": by_rating,
    }


def normalize_review(f: dict) -> dict:
    return {
        "id": f.get("id") or f.get("globalUserId"),
        "rating": f.get("productValuation") or f.get("valuation"),
        "date": f.get("createdDate") or f.get("date") or "",
        "verified": f.get("matchingSize") in ("ok", "small", "big") or bool(f.get("wbUserDetails")),
        "color": f.get("color") or "",
        "size": f.get("size") or "",
        "text": (f.get("text") or "").strip(),
        "pros": (f.get("pros") or "").strip(),
        "cons": (f.get("cons") or "").strip(),
        "answer": ((f.get("answer") or {}).get("text") or "").strip() if isinstance(f.get("answer"), dict) else "",
        "votes_useful": f.get("votes", {}).get("pluses") if isinstance(f.get("votes"), dict) else None,
        "matching_size": f.get("matchingSize") or "",
        "has_photo": bool(f.get("photo") or f.get("photos")),
        "has_video": bool(f.get("video") or f.get("videos")),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("article", help="Article ID or product URL")
    ap.add_argument("--out", default=None, help="Output JSON path")
    ap.add_argument(
        "--all-variations",
        action="store_true",
        help="Include reviews for all nm_id variations under the same imt (default: only the requested nm).",
    )
    args = ap.parse_args()

    nm = extract_article(args.article)
    print(f"nm = {nm}")

    card = get_card(nm)
    imt = get_imt_id(card)
    print(f"imt = {imt}")

    name = get_product_name(card)
    if name:
        print(f"product: {name}")

    raw = fetch_feedbacks(imt)
    all_feedbacks = raw.get("feedbacks") or []
    imt_total_on_wb = raw.get("feedbackCount") or len(all_feedbacks)
    print(f"imt total reviews on WB: {imt_total_on_wb}")
    print(f"feedbacks returned by API: {len(all_feedbacks)}")

    nm_set = {f.get("nmId") for f in all_feedbacks if f.get("nmId")}
    print(f"distinct nm_ids in response: {sorted(nm_set)}")

    if args.all_variations:
        feedbacks = all_feedbacks
        scope = "imt"
    else:
        feedbacks = [f for f in all_feedbacks if f.get("nmId") == nm]
        scope = "nm"
        print(f"filtered to nm={nm}: {len(feedbacks)} reviews")

    nm_dist = nm_distribution_from_response(raw, nm)
    summary = summarize(feedbacks)
    if nm_dist:
        nm_total_on_wb = sum(int(v) for v in nm_dist.values())
        # Override the rating distribution with the API-provided per-nm one (more complete than our 200-sample).
        summary["rating_distribution"] = {int(k): int(v) for k, v in nm_dist.items()}
        summary["nm_total_reviews_on_wb"] = nm_total_on_wb
        if nm_total_on_wb:
            summary["rating_avg"] = round(
                sum(int(k) * int(v) for k, v in nm_dist.items()) / nm_total_on_wb, 2
            )
    summary["scope"] = scope
    summary["imt_total_reviews_on_wb"] = imt_total_on_wb
    summary["api_returned"] = len(all_feedbacks)

    reviews = [normalize_review(f) for f in feedbacks]

    out_path = args.out or os.path.join(
        os.path.expanduser("~"), "Desktop", f"wb_{nm}_reviews.json"
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "nm_id": nm,
                "imt_id": imt,
                "product_name": name,
                "scope": scope,
                "imt_total_reviews_on_wb": imt_total_on_wb,
                "nm_total_reviews_on_wb": summary.get("nm_total_reviews_on_wb"),
                "summary": summary,
                "nm_ids_in_imt": sorted(nm_set),
                "reviews": reviews,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(f"\nDone. Saved to: {out_path}")
    print(
        f"  scope={scope}, total_in_file={summary['total']}, "
        f"avg={summary['rating_avg']}, with_text={summary['with_text']}, with_photo={summary['with_photo']}"
    )
    if summary.get("nm_total_reviews_on_wb"):
        print(
            f"  total reviews for nm={nm} on WB: {summary['nm_total_reviews_on_wb']} "
            f"(file contains a sample of the latest {summary['total']})"
        )


if __name__ == "__main__":
    main()
