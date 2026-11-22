"""ダッシュボードの「利益の推移と予測」「価格の推移」の表を作る(dashboard_snapshot.py から呼ぶ。読み取りのみ)。

出力(CSV):
  ship_trend   便ごと・日ごと。kind=actual は実績(その日までの累計利益と、その日の「全部売れたら」の見込み)、
               kind=plan は今日から先のプランごとの予測(累計利益の中央と幅)。
  price_trend  商品ごと・日ごと。自分の出品価格・カート価格・売れた数と平均価格(実績)、プランの価格(予測)。
  plans        便×プランの比較表(損益分岐を超える日・売り切る日・最終の利益とROI・目標との差)。

考え方(2026-11-15 CEOと決めた):
  - 累計利益 = その日までの手取り − 原価 − 国際送料・関税 − 保管料。0を超えた日が損益分岐。
  - 価格: 11/10以降は毎日の価格記録(price_snapshots)。それより前は自分の価格の記録が無いので、
    Keepaのカート価格(dashboard_keepa.json)で埋めて「推定」とする。
  - 手数料: 価格を変えると販売手数料(約15%)だけ動くとみなし、今の価格の実額から差分で近似する。
  - 売れるペース: 直近30日の販売数 ÷ 在庫があった日数。価格を変えたときは (新価格/基準価格)^-ELASTICITY 倍。
    さらにカート価格より2%以上高いときは ABOVE_BB 倍(基準価格がすでにカートより高い商品は二重に掛けない)。
    実績の無い商品は同じ種類(商品名の最初の語)の中央値、それも無ければ全体の中央値。幅は 0.5〜1.5 倍。
  - 保管料(予測だけ。過去分は収支ページと同じく入れない): Amazonの月額保管料(標準サイズ、1立方フィートあたり 1〜9月$0.78・10〜12月$2.40)を残りの在庫に日割り。
  - 予測するのは国際発送した便だけ。グラフに描く期間は「その便の決着まで」(CEO 11/15): プラン①の
    損益分岐を超える日と目標に届く日の遅いほう(目標に届かない便は9割売れる日)+14日。最短14日・最長365日。
    全プランで同じ期間。比較表も最長365日まで計算する。
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import ops_finance as of

USD_JPY = 150.0
DUTY_RATE = 0.125
REFERRAL = 0.15
ELASTICITY = 3.0
BAND = (0.5, 1.5)
ABOVE_BB = 0.3      # カート価格より2%以上高いと、カートをほぼ取れないので売れるペースを0.3倍とみなす(仮定)
SIM_DAYS = 365
MIN_HORIZON = 14
KEEPA_FILE = Path(__file__).with_name('dashboard_keepa.json')
PLANS = [
    ('now', '① 今のまま'),
    ('match', '② カートに合わせる'),
    ('floor', '③ 最低価格まで'),
    ('up10', '④ 値上げ10%'),
]


def d(s: str) -> date:
    return datetime.fromisoformat(s[:10]).date()


def storage_rate(day: date) -> float:
    return 2.40 if day.month >= 10 else 0.78


def r2(x):
    return None if x is None else round(x, 2)


def compute(ctx: dict, meta: dict) -> dict:
    today = d(ctx['today'])
    item_rows = [r for r in ctx['item_rows'] if r['cid']]
    ships = {x['n']: x for x in ctx['ship_rows']}
    targets = ctx['targets']
    events = ctx['events']
    fees = ctx['fees']
    intl_unit = ctx['intl_est']
    keepa = json.loads(KEEPA_FILE.read_text()) if KEEPA_FILE.exists() else {}
    elasticity = float(meta.get('forecast_elasticity', ELASTICITY))

    conn = sqlite3.connect(of.DB_PATH)
    our_by_day, bb_by_day = defaultdict(dict), defaultdict(dict)   # ASIN -> {日付: 価格}
    try:
        for asin, sku, day, our, bbv in conn.execute(
                'SELECT asin, sku, snapshot_on, our_price, buy_box_usd FROM price_snapshots ORDER BY snapshot_on'):
            main_sku, _ = ctx['price_of'](asin)
            if our is not None and (sku == main_sku or day not in our_by_day[asin]):
                our_by_day[asin][day] = ctx['capped'](sku, our)
            if bbv is not None:
                bb_by_day[asin][day] = bbv
    except sqlite3.OperationalError:
        pass
    since30 = (today - timedelta(days=30)).isoformat()
    in_stock_30 = of.in_stock_days_by_asin(conn, since30, today.isoformat())
    conn.close()
    first_snap = min((min(v) for v in our_by_day.values() if v), default=None)

    # ---------- 手数料の近似と損益分岐の価格 ----------
    base = {}
    for r in item_rows:
        if r['asin'] not in base:
            p0, f0 = ctx['base_price'](r['asin'])
            base[r['asin']] = (p0, f0)

    def fee_at(asin, p):
        p0, f0 = base.get(asin, (None, None))
        if p0 is None or f0 is None:
            return p * 0.45   # 手数料が取れない商品の目安
        return max(0.0, f0 + REFERRAL * (p - p0))

    cost_unit = {}
    for r in item_rows:
        cost_unit.setdefault(r['asin'], r['cost_jpy'] / USD_JPY)

    def breakeven_price(asin):
        p0, f0 = base.get(asin, (None, None))
        c = cost_unit[asin]
        need = c + intl_unit + c * DUTY_RATE
        if p0 is None or f0 is None:
            return need / 0.55
        return (need + f0 - REFERRAL * p0) / (1 - REFERRAL)

    def price_on(asin, day_s):
        """その日の自分の価格(推定かどうか)。"""
        if day_s in our_by_day[asin]:
            return our_by_day[asin][day_s], False
        if first_snap and day_s < first_snap:
            k = (keepa.get(asin) or {}).get('buy_box') or {}
            ks = [x for x in k if x <= day_s]
            if ks:
                return k[max(ks)], True
        prev = [x for x in our_by_day[asin] if x <= day_s]
        if prev:
            return our_by_day[asin][max(prev)], False
        return base.get(asin, (None,))[0], True

    def bb_on(asin, day_s):
        if day_s in bb_by_day[asin]:
            return bb_by_day[asin][day_s]
        k = (keepa.get(asin) or {}).get('buy_box') or {}
        ks = [x for x in k if x <= day_s]
        return k[max(ks)] if ks else None

    def cuft(asin):
        mm = (keepa.get(asin) or {}).get('package_mm') or []
        if len(mm) == 3 and all(mm):
            return mm[0] * mm[1] * mm[2] / 28316846.6
        return 0.0

    # ---------- 実績の推移(便ごと・日ごと) ----------
    ship_trend, price_trend, plans_out = [], [], []
    total_cost = {}
    shipped_on = {}
    for n, x in ships.items():
        s = x['s']
        cid = s['shipmentConfirmationId']
        so = (events.get(cid, {}).get('tnk_shipped') or {}).get('date')
        if not so:
            continue
        shipped_on[n] = d(so)
        total_cost[n] = (s['costUsd'] or 0) + (s['intlShippingUsd'] or 0) + (s['dutyUsd'] or 0)
    alloc_by_n = defaultdict(list)
    for a in ctx['alloc']:
        alloc_by_n[a['n']].append(a)
    storage_cum = defaultdict(float)
    for n, so in shipped_on.items():
        rows = [r for r in item_rows if r['n'] == n]
        day = so
        while day <= today:
            ds = day.isoformat()
            sold = [a for a in alloc_by_n[n] if a['date'] <= ds]
            net = sum(a['net'] for a in sold)
            est = False
            proj = net - total_cost[n]
            for r in rows:
                q_sold = sum(a['qty'] for a in sold if a['asin'] == r['asin'])
                left = r['sent'] - q_sold - r['lost']
                p, e = price_on(r['asin'], ds)
                est = est or e
                if p is not None and left > 0:
                    proj += left * (p - fee_at(r['asin'], p))
            ship_trend.append(dict(ship=x_ship(ships, n), date=ds, kind='actual', plan='', profit=r2(net - total_cost[n] - storage_cum[n]),
                                   profit_lo='', profit_hi='', projected=r2(proj - storage_cum[n]), est=1 if est else 0))
            day += timedelta(days=1)

    # ---------- 価格の推移(商品ごと・日ごと) ----------
    names = {}
    for r in item_rows:
        names.setdefault(r['asin'], r['product'])
    sales = defaultdict(lambda: defaultdict(lambda: [0, 0.0]))   # ASIN -> 日付 -> [個数, 売上]
    bbcur = ctx['bb']
    for o in ctx['sold_items']:
        if o['asin'] not in names or not o['quantity']:
            continue
        price, _ = of._estimated_price(o, bbcur)
        if price is None:
            continue
        x = sales[o['asin']][(o['purchase_date'] or '')[:10]]
        x[0] += o['quantity']
        x[1] += price * o['quantity']   # 価格は1個あたり
    first_day = min(shipped_on.values(), default=today)
    for asin, name in names.items():
        day = min((shipped_on[r['n']] for r in item_rows if r['asin'] == asin and r['n'] in shipped_on), default=first_day)
        while day <= today:
            ds = day.isoformat()
            p, e = price_on(asin, ds)
            sq, sv = sales[asin].get(ds, [0, 0.0])
            price_trend.append(dict(product=name, date=ds, kind='actual', plan='', our_price=r2(p) if not e else '',
                                    buy_box=r2(bb_on(asin, ds)), sold_qty=sq, sold_price=r2(sv / sq) if sq else '',
                                    est=1 if e else 0))
            day += timedelta(days=1)

    # ---------- 予測 ----------
    # 売れるペース(直近30日) と 基準価格(直近30日に売れた平均価格、無ければ今の価格)
    sold30 = defaultdict(int)
    rev30 = defaultdict(float)
    for o in ctx['sold_items']:
        if (o['purchase_date'] or '')[:10] > since30 and o['quantity']:
            price, _ = of._estimated_price(o, bbcur)
            sold30[o['asin']] += o['quantity']
            rev30[o['asin']] += (price or 0) * o['quantity']
    rate, p_ref = {}, {}
    for asin in names:
        days = in_stock_30.get(asin, 0)
        if days >= 3 and sold30[asin]:
            rate[asin] = sold30[asin] / days
        elif days >= 7:
            rate[asin] = 0.0 + 0.5 / days   # 在庫があっても売れていない: ごく小さいペース
        p_ref[asin] = rev30[asin] / sold30[asin] if sold30[asin] else base.get(asin, (None,))[0]
    group = lambda a: names[a].split(' ')[0]
    known = [v for v in rate.values() if v > 0]
    overall = statistics.median(known) if known else 0.1
    for asin in names:
        if asin not in rate:
            g = [rate[a] for a in rate if group(a) == group(asin) and rate[a] > 0]
            rate[asin] = statistics.median(g) if g else overall

    def plan_price(asin, plan):
        p0 = base.get(asin, (None,))[0]
        if p0 is None:
            return None
        be = breakeven_price(asin)
        bbv = bbcur.get(asin) or p0
        if plan == 'now':
            return p0
        if plan == 'match':
            return max(bbv, be)
        if plan == 'floor':
            return max((bbv + be) / 2, be + cost_unit[asin] * 0.2)
        if plan == 'up10':
            return p0 * 1.1
        return p0

    left_now = {}   # (n, asin) -> 残り
    for r in item_rows:
        left_now[(r['n'], r['asin'])] = left_now.get((r['n'], r['asin']), 0) + max(r['left'], 0)
    profit_now = {}
    for n in shipped_on:
        profit_now[n] = sum(a['net'] for a in alloc_by_n[n]) - total_cost[n] - storage_cum[n]

    for plan, plan_name in PLANS:
        prices = {a: plan_price(a, plan) for a in names}
        for asin, p in prices.items():
            if p is not None:
                price_trend.append(dict(product=names[asin], date=today.isoformat(), kind='plan', plan=plan, our_price=r2(p),
                                        buy_box='', sold_qty='', sold_price='', est=''))
        for mult_i, mult in enumerate((1.0,) + BAND):
            left = dict(left_now)
            cum = dict(profit_now)
            series = {n: [] for n in shipped_on}
            be_day, out_day, tg_day, p90_day = {}, {}, {}, {}
            tgt = {n: (targets.get(ships[n]['s']['shipmentConfirmationId']) or {}).get('profit_usd') for n in shipped_on}
            left0 = {n: sum(v for (m, _), v in left_now.items() if m == n) for n in shipped_on}
            for i in range(1, SIM_DAYS + 1):
                day = today + timedelta(days=i)
                for asin, p in prices.items():
                    if p is None:
                        continue
                    pr = p_ref.get(asin) or p
                    bbv = bbcur.get(asin)
                    above = lambda x: ABOVE_BB if bbv and x > bbv * 1.02 else 1.0
                    q = rate[asin] * mult * (p / pr) ** (-elasticity) * above(p) / above(pr)
                    unit_net = p - fee_at(asin, p)
                    for n in sorted(shipped_on):
                        if q <= 0:
                            break
                        k = (n, asin)
                        if left.get(k, 0) <= 0:
                            continue
                        tk = min(q, left[k])
                        left[k] -= tk
                        q -= tk
                        cum[n] += tk * unit_net
                for (n, asin), v in left.items():
                    if v > 0 and n in cum:
                        cum[n] -= v * cuft(asin) * storage_rate(day) / 30
                for n in shipped_on:
                    series[n].append(cum[n])
                    if n not in be_day and cum[n] >= 0:
                        be_day[n] = day
                    if tgt[n] is not None and n not in tg_day and cum[n] >= tgt[n]:
                        tg_day[n] = day
                    rest = sum(v for (m, _), v in left.items() if m == n)
                    if n not in p90_day and rest <= left0[n] * 0.1:
                        p90_day[n] = day
                    if n not in out_day and rest < 0.5:
                        out_day[n] = day
            if mult_i == 0:
                mid, mid_be, mid_out, mid_tg, mid_p90 = series, be_day, out_day, tg_day, p90_day
            elif mult_i == 1:
                lo, lo_be, lo_out, lo_tg = series, be_day, out_day, tg_day
            else:
                hi, hi_be, hi_out, hi_tg = series, be_day, out_day, tg_day
        if plan == 'now':   # グラフの期間はプラン①で決め、全プラン共通にする
            horizon_of = {}
            for n in shipped_on:
                ends = [mid_be.get(n) or today + timedelta(days=SIM_DAYS)]
                if tgt[n] is not None and profit_now[n] < tgt[n]:
                    ends.append(mid_tg.get(n) or mid_p90.get(n) or today + timedelta(days=SIM_DAYS))
                end = max(ends) + timedelta(days=14)
                horizon_of[n] = min(SIM_DAYS, max(MIN_HORIZON, (end - today).days))
        for n, so in shipped_on.items():
            horizon = horizon_of[n]
            for i in range(horizon):
                day = today + timedelta(days=i + 1)
                ship_trend.append(dict(ship=x_ship(ships, n), date=day.isoformat(), kind='plan', plan=plan,
                                       profit=r2(mid[n][i]), profit_lo=r2(lo[n][i]), profit_hi=r2(hi[n][i]),
                                       projected='', est=''))
            cid = ships[n]['s']['shipmentConfirmationId']
            t = targets.get(cid) or {}
            final = mid[n][-1]
            md_ = lambda x: f'{x.month}/{x.day}' if x else ''
            plans_out.append(dict(
                ship=x_ship(ships, n), plan=plan, plan_name=plan_name,
                breakeven_date=md_(mid_be.get(n)) if profit_now[n] < 0 else '達成済み',
                breakeven_range=f"{md_(hi_be.get(n)) or '1年超'}〜{md_(lo_be.get(n)) or '1年超'}" if profit_now[n] < 0 else '',
                sellout_date=md_(mid_out.get(n)) or '1年で売り切れない',
                sellout_range=f"{md_(hi_out.get(n)) or '1年超'}〜{md_(lo_out.get(n)) or '1年超'}",
                final_profit=r2(final), final_lo=r2(lo[n][-1]), final_hi=r2(hi[n][-1]),
                roi=round(final / total_cost[n], 4) if total_cost[n] else '',
                target_profit=t.get('profit_usd', ''),
                diff_target=r2(final - t['profit_usd']) if t else '',
                target_date=('' if not t else '達成済み' if profit_now[n] >= t['profit_usd'] else md_(mid_tg.get(n)) or '届かない'),
                target_range=(f"{md_(hi_tg.get(n)) or '届かない'}〜{md_(lo_tg.get(n)) or '届かない'}"
                              if t and profit_now[n] < t['profit_usd'] else ''),
                breakeven_iso=(mid_be.get(n).isoformat() if mid_be.get(n) and profit_now[n] < 0 else ''),
                target_iso=(mid_tg.get(n).isoformat() if t and mid_tg.get(n) and profit_now[n] < t['profit_usd'] else ''),
                horizon_days=horizon))
    sales_trend = build_sales_trend(ctx, ship_trend)
    return {'ship_trend': ship_trend, 'price_trend': price_trend, 'plans': plans_out, 'sales_trend': sales_trend}


def build_sales_trend(ctx: dict, ship_trend: list) -> list:
    """1ページ目の累計グラフ用。日ごとの売上・利益と累計。
    profit = 売れた分だけの利益(手取り − 原価 − 国際送料・関税。1個ずつ)。cash = 便ごとの累計利益(ship_trend)の合計で、
    発送した分の原価・送料を全部引いた「投下コスト込み」。"""
    from dashboard_snapshot import DUTY_RATE, USD_JPY
    unit, unit_pre = {}, {}
    for r in ctx['item_rows']:
        if r['cid']:
            c = (r['cost_jpy'] or 0) / USD_JPY
            unit_pre[(r['n'], r['asin'])] = c   # 送料前の利益用(国際送料・関税を引かない。AWS側の定義)
            unit[(r['n'], r['asin'])] = c + ctx['intl_est'] + c * DUTY_RATE   # ストックの表と同じ見積もり(1個$0.742＋原価の12.5%)
    day = defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0.0])   # 個数, 売上, 手取り, 利益(送料後), 利益(送料前)
    for a in ctx['alloc']:
        if not a['date']:
            continue
        x = day[a['date']]
        x[0] += a['qty']
        x[1] += a['rev']
        x[2] += a['net']
        x[3] += a['net'] - a['qty'] * unit.get((a['n'], a['asin']), 0.0)
        x[4] += a['net'] - a['qty'] * unit_pre.get((a['n'], a['asin']), 0.0)
    cash_by = defaultdict(dict)
    for r in ship_trend:
        if r['kind'] == 'actual':
            cash_by[r['ship']][r['date']] = r['profit']
    first = min([k for k in day] + [k for v in cash_by.values() for k in v], default=None)
    if not first:
        return []
    out, cs, cp, cpp = [], 0.0, 0.0, 0.0
    cur, end = d(first), d(ctx['today'])
    while cur <= end:
        ds = cur.isoformat()
        q, rev, net, pr, pre = day.get(ds, [0, 0.0, 0.0, 0.0, 0.0])
        cs += rev
        cp += pr
        cpp += pre
        cash = sum(v[max(k for k in v if k <= ds)] for v in cash_by.values() if any(k <= ds for k in v))
        out.append(dict(date=ds, qty=q, sales=r2(rev), profit=r2(pr), cum_sales=r2(cs), cum_profit=r2(cp), cum_cash=r2(cash),
                        profit_pre=r2(pre), cum_profit_pre=r2(cpp)))
        cur += timedelta(days=1)
    return out


def x_ship(ships, n):
    return ships[n]['ship']
