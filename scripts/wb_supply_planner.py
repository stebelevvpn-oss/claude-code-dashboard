"""Plan supplies to Wildberries fulfilment centres.

Methodology
-----------
1. Pull sales for the analysis window and current stock snapshot from the
   WB Seller statistics API.
2. Group warehouses into shipment clusters (built-in mapping, overridable
   via --clusters JSON).
3. For every SKU compute:
     * daily sales velocity over the analysis window;
     * share of sales per cluster (regional demand proxy);
     * share of each warehouse inside its cluster, evaluated only on
       "clean" days — days when every warehouse in the cluster had any
       sale (used here as a presence proxy because Seller API does not
       expose historical stock by date).
4. Target total stock per SKU = velocity * --turnover-days.
   Target per warehouse = target_total * cluster_share * warehouse_share.
   Quantity to supply per warehouse = max(0, target - current).

Output: a CSV plan and a JSON summary on the user's Desktop (override
with --out).

Authentication: token is read from --token, $env:WB_API_TOKEN, or
%USERPROFILE%\\.wb_token (single-line file), in that order.

Usage:
    python wb_supply_planner.py --turnover-days 30 [--analysis-days 60] \
        [--articles 123,456] [--out path.csv] [--clusters mapping.json]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

# Windows consoles default to cp1251/cp866 which cannot encode many of
# the characters we print (Cyrillic, arrows). Force UTF-8 with
# replacement so logging never crashes the run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass


API_BASE = "https://statistics-api.wildberries.ru"
SALES_PATH = "/api/v1/supplier/sales"
STOCKS_PATH = "/api/v1/supplier/stocks"

UA = "wb-supply-planner/1.0"


# Built-in warehouse-to-cluster mapping. Match is by case-insensitive
# substring on warehouseName, in order — first hit wins. Extend or
# replace via --clusters mapping.json (same structure: cluster -> list
# of name fragments).
DEFAULT_CLUSTERS: dict[str, list[str]] = {
    "Москва": [
        "коледино", "электросталь", "подольск", "пушкино", "алексин",
        "тула", "чехов", "радумля", "белые столбы", "серпухов",
        "обухово", "внуково", "дмитров", "электроугли", "сабурово",
        "софьино", "долгопрудный", "истра", "коптево", "белая дача",
        "ногинск", "вёшки", "домодедово", "сынково", "москва",
    ],
    "Санкт-Петербург": [
        "шушары", "уткина заводь", "уткина-заводь", "санкт-петербург",
        "спб", "колпино", "парголово", "невский", "московское шоссе 177",
    ],
    "Юг": [
        "краснодар", "невинномысск", "ростов", "армавир", "адыгея",
        "пятигорск", "владикавказ", "астрахань", "волгоград",
        "махачкала", "ставрополь",
    ],
    "Урал": [
        "екатеринбург", "челябинск", "касимово", "пермь", "сарапул",
        "ижевск", "тюмень", "сургут",
    ],
    "Поволжье": [
        "казань", "самара", "тольятти", "котовск", "уфа", "тамбов",
        "саратов", "оренбург", "набережные челны", "пенза", "кузнецк",
        "нижнекамск", "ульяновск",
    ],
    "Сибирь": [
        "новосибирск", "омск", "красноярск", "барнаул", "кемерово",
        "иркутск", "томск", "братск", "абакан", "горно-алтайск",
        "кызыл", "бийск",
    ],
    "Северо-Запад": [
        "архангельск", "вологда", "сыктывкар",
    ],
    "Дальний Восток": [
        "хабаровск", "владивосток",
    ],
    "Калининград": [
        "калининград",
    ],
    "ЦФО": [
        "владимир", "воронеж", "рязань", "курск", "брянск",
        "ярославль", "иваново", "тверь", "старый оскол", "белгород",
        "липецк", "орёл",
    ],
    "Беларусь": [
        "минск", "гомель", "брест", "витебск", "орша",
    ],
    "Казахстан": [
        "алматы", "астана", "нур-султан", "шымкент", "атакент",
        "актобе", "караганд",
    ],
    "Узбекистан": [
        "ташкент",
    ],
    "Армения": [
        "ереван",
    ],
    "Крым": [
        "крым", "симферополь", "севастополь",
    ],
}


# Warehouse-name fragments that mark a *non-physical* destination —
# virtual zones, sorting centres, hubs. Sellers cannot ship inbound
# supply to these; they can only act as outbound delivery points.
# We exclude them from cluster membership so they neither break the
# "clean days" intersection nor appear as planned supply targets.
NON_FC_FRAGMENTS = ("виртуальный", "сц ", "сц.", "ск ")


def is_real_fc(warehouse: str) -> bool:
    if not warehouse:
        return False
    wh = warehouse.lower().strip()
    for frag in NON_FC_FRAGMENTS:
        if wh.startswith(frag) or frag in f" {wh} ":
            return False
    return True


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------- token resolution ----------

def resolve_token(cli_token: str | None) -> str:
    if cli_token:
        return cli_token.strip()
    env = os.environ.get("WB_API_TOKEN")
    if env:
        return env.strip()
    token_file = os.path.join(os.path.expanduser("~"), ".wb_token")
    if os.path.exists(token_file):
        with open(token_file, encoding="utf-8") as f:
            return f.read().strip()
    raise SystemExit(
        "WB API token not found. Provide --token, set $env:WB_API_TOKEN, "
        "or save the token to %USERPROFILE%\\.wb_token (single line)."
    )


# ---------- HTTP ----------

def api_get(path: str, params: dict, token: str, timeout: int = 90) -> list | dict:
    url = API_BASE + path + "?" + urllib.parse.urlencode(params)
    headers = {
        "Authorization": token,
        "User-Agent": UA,
        "Accept": "application/json",
    }
    attempt = 0
    while True:
        attempt += 1
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                return json.loads(body.decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 6:
                wait = 2 ** attempt
                log(f"[!] 429 from {path}, sleeping {wait}s (attempt {attempt})")
                time.sleep(wait)
                continue
            if e.code == 401:
                raise SystemExit("WB API: 401 Unauthorized — token is invalid or expired.")
            if e.code == 403:
                raise SystemExit(
                    "WB API: 403 Forbidden — token lacks the 'Statistics' scope. "
                    "Re-issue it in Личный кабинет -> Настройки -> Доступ к API."
                )
            raise
        except urllib.error.URLError as e:
            if attempt < 4:
                log(f"[!] network error: {e.reason}, retrying in 3s")
                time.sleep(3)
                continue
            raise


# ---------- data fetch ----------

def fetch_sales(token: str, days: int) -> list[dict]:
    """Return raw sale records for the last `days` days."""
    date_from = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    log(f"[*] sales: dateFrom={date_from}")
    rows = api_get(SALES_PATH, {"dateFrom": date_from, "flag": 0}, token)
    if not isinstance(rows, list):
        raise SystemExit(f"sales: unexpected response: {str(rows)[:200]}")
    log(f"[*] sales: {len(rows)} rows")
    return rows


def fetch_stocks(token: str) -> list[dict]:
    date_from = (dt.datetime.now() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    log(f"[*] stocks: dateFrom={date_from}")
    rows = api_get(STOCKS_PATH, {"dateFrom": date_from}, token)
    if not isinstance(rows, list):
        raise SystemExit(f"stocks: unexpected response: {str(rows)[:200]}")
    log(f"[*] stocks: {len(rows)} rows")
    return rows


# ---------- clustering ----------

def build_cluster_resolver(mapping: dict[str, list[str]]):
    pairs = []  # list of (lowercase_fragment, cluster)
    for cluster, fragments in mapping.items():
        for frag in fragments:
            pairs.append((frag.lower(), cluster))

    def resolve(warehouse: str) -> str:
        if not warehouse:
            return "Неопределён"
        wh = warehouse.lower()
        for frag, cluster in pairs:
            if frag in wh:
                return cluster
        return f"Прочие/{warehouse}"

    return resolve


# ---------- core computation ----------

def normalize_sale(row: dict) -> dict | None:
    """Filter rows to actual sales (saleID starts with 'S')."""
    sale_id = str(row.get("saleID", ""))
    if not sale_id.startswith("S"):
        return None
    nm = row.get("nmId")
    wh = row.get("warehouseName") or ""
    date = (row.get("date") or "")[:10]
    if not nm or not wh or not date:
        return None
    return {
        "nm": int(nm),
        "warehouse": wh,
        "date": date,
        "qty": 1,  # sales endpoint returns one row per item
        "supplier_article": row.get("supplierArticle") or "",
        "category": row.get("subject") or "",
        "brand": row.get("brand") or "",
    }


def compute_plan(
    sales: list[dict],
    stocks: list[dict],
    cluster_of,
    turnover_days: int,
    analysis_days: int,
    article_filter: set[int] | None,
    seed_empty: bool = False,
    velocity_multiplier: float = 1.0,
    velocity_override: dict[int, float] | None = None,
    min_per_cluster: int = 0,
    min_per_warehouse: int = 0,
    blocked_fragments: list[str] | None = None,
):
    velocity_override = velocity_override or {}
    blocked_fragments = blocked_fragments or []
    # If the user demands a per-FC minimum, we must enumerate all
    # physical FCs as candidate destinations even when the SKU has no
    # sales there yet — otherwise there is nothing to lift to the
    # minimum. Implicitly turn on the seed pathway in that case.
    if min_per_warehouse > 0 or min_per_cluster > 0:
        seed_empty = True

    def is_blocked(name: str) -> bool:
        if not blocked_fragments:
            return False
        n = name.lower()
        return any(frag in n for frag in blocked_fragments)

    # ---- index sales ----
    parsed = []
    for r in sales:
        rec = normalize_sale(r)
        if rec is None:
            continue
        if article_filter and rec["nm"] not in article_filter:
            continue
        parsed.append(rec)

    # qty[nm][warehouse]
    qty = defaultdict(lambda: defaultdict(int))
    qty_by_nm = defaultdict(int)
    qty_by_nm_cluster = defaultdict(lambda: defaultdict(int))
    qty_by_nm_cluster_wh = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    nm_meta: dict[int, dict] = {}
    warehouses_seen: set[str] = set()

    # active[warehouse] = set(date strings) where ANY SKU sold
    active = defaultdict(set)
    cluster_warehouses = defaultdict(set)

    # qty per (nm, warehouse, date) — needed for clean-period filter
    qty_per_day = defaultdict(int)  # key = (nm, warehouse, date)

    for r in parsed:
        nm = r["nm"]; wh = r["warehouse"]; d = r["date"]
        cl = cluster_of(wh)
        qty[nm][wh] += 1
        qty_by_nm[nm] += 1
        qty_by_nm_cluster[nm][cl] += 1
        qty_by_nm_cluster_wh[nm][cl][wh] += 1
        active[wh].add(d)
        warehouses_seen.add(wh)
        qty_per_day[(nm, wh, d)] += 1
        # Only physical FCs are eligible inbound destinations; virtual
        # zones and sorting centres are sales-only and would otherwise
        # break the clean-days intersection (they almost never ship).
        # Blocked warehouses (closed for inbound) are also excluded.
        if is_real_fc(wh) and not is_blocked(wh):
            cluster_warehouses[cl].add(wh)
        if nm not in nm_meta:
            nm_meta[nm] = {
                "supplier_article": r["supplier_article"],
                "category": r["category"],
                "brand": r["brand"],
            }

    # ---- index stocks ----
    stock = defaultdict(lambda: defaultdict(int))   # stock[nm][warehouse]
    stock_total = defaultdict(int)
    for s in stocks:
        nm = s.get("nmId")
        if nm is None:
            continue
        nm = int(nm)
        if article_filter and nm not in article_filter:
            continue
        wh = s.get("warehouseName") or ""
        q = int(s.get("quantity") or 0)  # available to clients
        if q <= 0 or not wh:
            continue
        stock[nm][wh] += q
        stock_total[nm] += q
        warehouses_seen.add(wh)
        if is_real_fc(wh) and not is_blocked(wh):
            cluster_warehouses[cluster_of(wh)].add(wh)
        if nm not in nm_meta:
            nm_meta[nm] = {
                "supplier_article": s.get("supplierArticle") or "",
                "category": s.get("subject") or "",
                "brand": s.get("brand") or "",
            }

    # ---- clean periods per cluster ----
    # A day is "clean" for cluster C if every warehouse in C had ANY sale that day.
    # For singleton clusters every active day is clean.
    cluster_clean_days = {}
    for cl, whs in cluster_warehouses.items():
        if not whs:
            cluster_clean_days[cl] = set()
            continue
        if len(whs) == 1:
            (only,) = whs
            cluster_clean_days[cl] = set(active[only])
            continue
        all_days = set().union(*[active[w] for w in whs])
        clean = {d for d in all_days if all(d in active[w] for w in whs)}
        cluster_clean_days[cl] = clean

    # ---- catalog-wide baselines (used by --seed-empty-clusters) ----
    # Share of each cluster across the WHOLE catalogue, and share of
    # each warehouse inside each cluster — used as a fallback for SKUs
    # that have never been sold from that cluster/warehouse but where
    # the seller still wants to seed inventory there.
    catalog_total = sum(qty_by_nm.values()) or 0
    cluster_baseline: dict[str, float] = {}
    wh_baseline: dict[str, dict[str, float]] = {}
    if catalog_total > 0:
        cluster_totals: dict[str, int] = defaultdict(int)
        wh_totals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for nm_, by_cl in qty_by_nm_cluster.items():
            for cl_, c_qty in by_cl.items():
                if cl_ not in cluster_warehouses:
                    continue  # ignore non-FC clusters
                cluster_totals[cl_] += c_qty
        for nm_, by_cl in qty_by_nm_cluster_wh.items():
            for cl_, by_wh in by_cl.items():
                if cl_ not in cluster_warehouses:
                    continue
                for w_, w_qty in by_wh.items():
                    if w_ in cluster_warehouses[cl_]:
                        wh_totals[cl_][w_] += w_qty
        catalog_fc_total = sum(cluster_totals.values()) or 1
        for cl_, c_total in cluster_totals.items():
            cluster_baseline[cl_] = c_total / catalog_fc_total
        for cl_, by_wh in wh_totals.items():
            cl_sum = sum(by_wh.values()) or 1
            wh_baseline[cl_] = {w_: q / cl_sum for w_, q in by_wh.items()}

    # ---- per-SKU: warehouse share inside cluster, on clean days ----
    plan_rows = []
    summary_rows = []
    nms = sorted(set(qty_by_nm) | set(stock_total))

    for nm in nms:
        meta = nm_meta.get(nm, {})
        total_sales = qty_by_nm.get(nm, 0)
        observed_velocity = total_sales / analysis_days if analysis_days > 0 else 0.0
        # Manual velocity override takes precedence (e.g. for promotions
        # / pre-sale planning). Otherwise scale the observed velocity
        # by the global multiplier.
        if nm in velocity_override:
            velocity = velocity_override[nm]
            velocity_source = "override"
        else:
            velocity = observed_velocity * velocity_multiplier
            velocity_source = "scaled" if velocity_multiplier != 1.0 else "observed"
        target_total = velocity * turnover_days
        current_total = stock_total.get(nm, 0)
        supply_total_estimate = max(0.0, target_total - current_total)

        # Net-supply gate: if existing stock already covers the target,
        # we do not generate inbound supply rows — that would overstock
        # the seller. Such SKUs are still listed in the summary so the
        # operator can see they are intentionally excluded.
        if target_total <= current_total:
            summary_rows.append({
                "nm_id": nm,
                "supplier_article": meta.get("supplier_article", ""),
                "category": meta.get("category", ""),
                "brand": meta.get("brand", ""),
                "daily_sales": round(velocity, 3),
                "velocity_source": velocity_source,
                "current_stock_total": current_total,
                "target_total": math.ceil(target_total),
                "supply_total_estimate": 0,
                "supply_total_planned": 0,
                "clusters": "",
            })
            continue

        # cluster share: by all sales (not just clean days)
        cluster_share: dict[str, float] = {}
        if total_sales > 0:
            for cl, c_qty in qty_by_nm_cluster[nm].items():
                if cl in cluster_warehouses:
                    cluster_share[cl] = c_qty / total_sales

        # --- seed empty clusters with catalog-wide baseline ---
        seeded_clusters: set[str] = set()
        if seed_empty:
            for cl in cluster_warehouses:
                if cluster_share.get(cl, 0) <= 0:
                    base = cluster_baseline.get(cl, 0)
                    if base > 0:
                        cluster_share[cl] = base
                        seeded_clusters.add(cl)
            s = sum(cluster_share.values())
            if s > 0:
                cluster_share = {cl: v / s for cl, v in cluster_share.items()}

        # warehouse share inside each cluster
        wh_share: dict[str, dict[str, float]] = {}
        wh_share_basis: dict[str, str] = {}  # 'clean' | 'all' | 'single' | 'even' | 'seeded'
        for cl, whs in cluster_warehouses.items():
            if not whs:
                continue
            if len(whs) == 1:
                (only,) = whs
                wh_share.setdefault(cl, {})[only] = 1.0
                wh_share_basis[cl] = "single"
                continue
            clean_days = cluster_clean_days[cl]
            clean_qty = defaultdict(int)
            clean_total = 0
            for w in whs:
                for d in clean_days:
                    q = qty_per_day.get((nm, w, d), 0)
                    clean_qty[w] += q
                    clean_total += q
            if clean_total > 0:
                wh_share[cl] = {w: clean_qty[w] / clean_total for w in whs if clean_qty[w] > 0}
                wh_share_basis[cl] = "clean"
            else:
                # Use cluster-wide sales total as the denominator (it
                # includes sales routed through virtual zones — they
                # signal demand even though we cannot ship inbound
                # there). Then renormalise across the surviving
                # physical FCs so the percentages add up to 100%.
                raw = {
                    w: qty_by_nm_cluster_wh[nm][cl].get(w, 0)
                    for w in whs
                }
                phys_total = sum(raw.values())
                if phys_total > 0:
                    wh_share[cl] = {w: q / phys_total for w, q in raw.items() if q > 0}
                    wh_share_basis[cl] = "all"
                else:
                    # demand exists in the cluster but only via virtual
                    # zones — distribute evenly across physical FCs.
                    wh_share[cl] = {w: 1.0 / len(whs) for w in whs}
                    wh_share_basis[cl] = "even"

            # --- seed empty warehouses inside the cluster ---
            if seed_empty:
                bw = wh_baseline.get(cl, {})
                missing = [w for w in whs if wh_share[cl].get(w, 0) <= 0 and bw.get(w, 0) > 0]
                if missing:
                    for w in missing:
                        wh_share[cl][w] = bw[w]
                    s = sum(wh_share[cl].values())
                    if s > 0:
                        wh_share[cl] = {w: v / s for w, v in wh_share[cl].items()}
                    if cl in seeded_clusters or wh_share_basis[cl] in ("even",):
                        wh_share_basis[cl] = "seeded"
                    else:
                        wh_share_basis[cl] = wh_share_basis[cl] + "+seeded"

        # ---- per-warehouse target & deficit ----
        # First compute the float target per warehouse from the share
        # tree, then distribute the integer budget (ceil of the global
        # deficit) using Largest-Remainder Method so that the sum of
        # to_supply values exactly equals the budget — no inflation
        # from per-row ceiling on small SKUs.
        weighted = []  # list of (cl, w, target_w_real, current_w)
        for cl, share_c in cluster_share.items():
            for w, share_w in wh_share.get(cl, {}).items():
                target_w_real = target_total * share_c * share_w
                current_w = stock[nm].get(w, 0)
                weighted.append((cl, w, target_w_real, current_w))

        deficits = [max(0.0, t - c) for (_, _, t, c) in weighted]
        deficit_sum = sum(deficits)
        budget = math.ceil(max(0.0, target_total - current_total))

        if budget <= 0 or deficit_sum <= 0:
            allocation = [0] * len(weighted)
        else:
            raw = [budget * (d / deficit_sum) for d in deficits]
            floor_alloc = [int(math.floor(x)) for x in raw]
            remaining = budget - sum(floor_alloc)
            # distribute the integer remainder to rows with the largest
            # fractional parts; ties broken by larger absolute deficit.
            order = sorted(
                range(len(raw)),
                key=lambda i: (raw[i] - math.floor(raw[i]), deficits[i]),
                reverse=True,
            )
            for i in range(remaining):
                floor_alloc[order[i % len(order)]] += 1
            allocation = floor_alloc

        # ---- localization minimums (override the LR budget) ----
        # These lifts are intentional additions ON TOP of the velocity-
        # based budget — the seller has chosen wider geography over a
        # tight bill of materials. Applied only to SKUs that already
        # passed the net-supply gate.
        if min_per_warehouse > 0:
            for i, (cl, w, _, current_w) in enumerate(weighted):
                lift = min_per_warehouse - (current_w + allocation[i])
                if lift > 0:
                    allocation[i] += lift

        if min_per_cluster > 0:
            from collections import defaultdict as _dd
            cluster_alloc = _dd(int)
            cluster_current = _dd(int)
            cluster_indices: dict[str, list[int]] = _dd(list)
            for i, (cl, w, _, current_w) in enumerate(weighted):
                cluster_alloc[cl] += allocation[i]
                cluster_current[cl] += current_w
                cluster_indices[cl].append(i)
            for cl, indices in cluster_indices.items():
                lift = min_per_cluster - (cluster_alloc[cl] + cluster_current[cl])
                if lift <= 0 or not indices:
                    continue
                # park the extra units on the warehouse with the highest
                # share in this cluster — it is the natural anchor.
                best = max(indices, key=lambda i: wh_share.get(cl, {}).get(weighted[i][1], 0))
                allocation[best] += lift

        sku_plan = []
        for (cl, w, target_w_real, current_w), to_supply in zip(weighted, allocation):
            share_c = cluster_share.get(cl, 0.0)
            share_w = wh_share.get(cl, {}).get(w, 0.0)
            sku_plan.append({
                "nm_id": nm,
                "supplier_article": meta.get("supplier_article", ""),
                "category": meta.get("category", ""),
                "brand": meta.get("brand", ""),
                "cluster": cl,
                "warehouse": w,
                "daily_sales": round(velocity, 3),
                "current_stock_total": current_total,
                "target_total": math.ceil(target_total),
                "current_stock_warehouse": current_w,
                "target_warehouse": math.ceil(target_w_real),
                "to_supply": to_supply,
                "cluster_share_pct": round(share_c * 100, 1),
                "warehouse_share_pct": round(share_w * 100, 1),
                "share_basis": wh_share_basis.get(cl, ""),
            })

        # also surface clusters where SKU has zero sales but we still might
        # want to seed (skip by default — shown only in summary)

        plan_rows.extend(sku_plan)

        summary_rows.append({
            "nm_id": nm,
            "supplier_article": meta.get("supplier_article", ""),
            "category": meta.get("category", ""),
            "brand": meta.get("brand", ""),
            "daily_sales": round(velocity, 3),
            "velocity_source": velocity_source,
            "current_stock_total": current_total,
            "target_total": math.ceil(target_total),
            "supply_total_estimate": math.ceil(supply_total_estimate),
            "supply_total_planned": sum(r["to_supply"] for r in sku_plan),
            "clusters": ", ".join(sorted(cluster_share)),
        })

    return plan_rows, summary_rows, cluster_clean_days, cluster_warehouses


# ---------- output ----------

def write_csv(path: str, rows: list[dict], fieldnames: list[str]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    p = argparse.ArgumentParser(description="WB supply planner")
    p.add_argument("--turnover-days", type=int, required=True,
                   help="target days of cover per SKU across all FCs")
    p.add_argument("--analysis-days", type=int, default=60,
                   help="how many days of sales history to use (default 60)")
    p.add_argument("--articles", type=str, default="",
                   help="optional comma-separated nm_id filter")
    p.add_argument("--token", type=str, default=None)
    p.add_argument("--out", type=str, default=None,
                   help="output CSV path; defaults to Desktop/wb_supply_plan_<date>.csv")
    p.add_argument("--clusters", type=str, default=None,
                   help="JSON file overriding warehouse -> cluster mapping")
    p.add_argument("--include-zeros", action="store_true",
                   help="keep rows where to_supply == 0")
    p.add_argument("--seed-empty-clusters", action="store_true",
                   help="seed clusters/warehouses with no sales history "
                        "for the SKU using the catalogue-wide baseline "
                        "(useful when launching the SKU into new regions)")
    p.add_argument("--velocity-multiplier", type=float, default=1.0,
                   help="scale observed sales velocity by this factor "
                        "before planning (e.g. 1.5 to plan a +50%% promo)")
    p.add_argument("--velocity-override", type=str, default="",
                   help="manual velocity in units/day for specific SKUs, "
                        'comma-separated as "nm_id=value"; takes priority '
                        "over --velocity-multiplier for those SKUs")
    p.add_argument("--min-per-cluster", type=int, default=0,
                   help="guarantee at least this many units present per "
                        "cluster (current stock + supply); lifts the budget")
    p.add_argument("--min-per-warehouse", type=int, default=0,
                   help="guarantee at least this many units present per "
                        "physical FC; lifts the budget")
    p.add_argument("--blocked-warehouses", type=str, default="",
                   help="comma-separated name fragments of FCs closed for "
                        "inbound supply; their share is reallocated to the "
                        "remaining FCs of the same cluster (or other "
                        "clusters if the cluster empties)")
    args = p.parse_args()

    if args.turnover_days <= 0:
        raise SystemExit("--turnover-days must be > 0")
    if args.analysis_days <= 0:
        raise SystemExit("--analysis-days must be > 0")

    token = resolve_token(args.token)

    article_filter = None
    if args.articles.strip():
        article_filter = set()
        for chunk in args.articles.split(","):
            chunk = chunk.strip()
            if chunk:
                article_filter.add(int(chunk))

    cluster_mapping = DEFAULT_CLUSTERS
    if args.clusters:
        with open(args.clusters, encoding="utf-8") as f:
            cluster_mapping = json.load(f)
    cluster_of = build_cluster_resolver(cluster_mapping)

    velocity_override: dict[int, float] = {}
    if args.velocity_override.strip():
        for chunk in args.velocity_override.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "=" not in chunk:
                raise SystemExit(
                    f'Bad --velocity-override entry "{chunk}"; expected nm_id=value'
                )
            nm_str, val_str = chunk.split("=", 1)
            try:
                velocity_override[int(nm_str.strip())] = float(val_str.strip())
            except ValueError as e:
                raise SystemExit(f'Bad --velocity-override entry "{chunk}": {e}')

    blocked_fragments = [
        x.strip().lower() for x in args.blocked_warehouses.split(",") if x.strip()
    ]

    if args.velocity_multiplier <= 0:
        raise SystemExit("--velocity-multiplier must be > 0")
    if args.min_per_cluster < 0 or args.min_per_warehouse < 0:
        raise SystemExit("--min-per-* must be >= 0")

    sales = fetch_sales(token, args.analysis_days)
    stocks = fetch_stocks(token)

    plan_rows, summary_rows, clean_days, cluster_warehouses = compute_plan(
        sales, stocks, cluster_of,
        turnover_days=args.turnover_days,
        analysis_days=args.analysis_days,
        article_filter=article_filter,
        seed_empty=args.seed_empty_clusters,
        velocity_multiplier=args.velocity_multiplier,
        velocity_override=velocity_override,
        min_per_cluster=args.min_per_cluster,
        min_per_warehouse=args.min_per_warehouse,
        blocked_fragments=blocked_fragments,
    )

    if not args.include_zeros:
        plan_rows = [r for r in plan_rows if r["to_supply"] > 0]

    plan_rows.sort(key=lambda r: (r["nm_id"], r["cluster"], r["warehouse"]))
    summary_rows.sort(key=lambda r: -r["supply_total_planned"])

    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    os.makedirs(desktop, exist_ok=True)
    today = dt.date.today().strftime("%Y-%m-%d")
    out_csv = args.out or os.path.join(desktop, f"wb_supply_plan_{today}.csv")
    summary_csv = os.path.splitext(out_csv)[0] + "_summary.csv"
    summary_json = os.path.splitext(out_csv)[0] + "_summary.json"

    plan_fields = [
        "nm_id", "supplier_article", "brand", "category",
        "cluster", "warehouse",
        "daily_sales", "current_stock_total", "target_total",
        "current_stock_warehouse", "target_warehouse", "to_supply",
        "cluster_share_pct", "warehouse_share_pct", "share_basis",
    ]
    summary_fields = [
        "nm_id", "supplier_article", "brand", "category",
        "daily_sales", "velocity_source",
        "current_stock_total", "target_total",
        "supply_total_estimate", "supply_total_planned", "clusters",
    ]

    write_csv(out_csv, plan_rows, plan_fields)
    write_csv(summary_csv, summary_rows, summary_fields)

    diagnostics = {
        "turnover_days": args.turnover_days,
        "analysis_days": args.analysis_days,
        "seed_empty_clusters": bool(args.seed_empty_clusters),
        "velocity_multiplier": args.velocity_multiplier,
        "velocity_overrides": velocity_override,
        "min_per_cluster": args.min_per_cluster,
        "min_per_warehouse": args.min_per_warehouse,
        "blocked_warehouses": blocked_fragments,
        "sales_rows_total": len(sales),
        "skus_planned": len(summary_rows),
        "plan_rows": len(plan_rows),
        "supply_total_units": sum(r["to_supply"] for r in plan_rows),
        "clusters": {
            cl: {
                "warehouses": sorted(list(whs)),
                "clean_days": len(clean_days.get(cl, set())),
            }
            for cl, whs in cluster_warehouses.items()
        },
        "files": {
            "plan_csv": out_csv,
            "summary_csv": summary_csv,
            "summary_json": summary_json,
        },
    }
    with open(summary_json, "w", encoding="utf-8") as f:
        json.dump(diagnostics, f, ensure_ascii=False, indent=2)

    print(out_csv)
    log(f"[+] plan: {out_csv} ({len(plan_rows)} rows, "
        f"{diagnostics['supply_total_units']} units total)")
    log(f"[+] summary: {summary_csv}")
    log(f"[+] diagnostics: {summary_json}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        log(f"[x] {type(e).__name__}: {e}")
        sys.exit(2)
