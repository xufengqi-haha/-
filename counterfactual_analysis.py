import json, math
from datetime import datetime

def haversine_km(lat1, lng1, lat2, lng2):
    r = 6371.0
    p1, l1, p2, l2 = math.radians(lat1), math.radians(lng1), math.radians(lat2), math.radians(lng2)
    dp, dl = p2 - p1, l2 - l1
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * r * math.asin(math.sqrt(min(1.0, a)))

def to_sim_min(s):
    epoch = datetime(2026,3,1,0,0,0)
    dt = datetime.strptime(s.strip(), '%Y-%m-%d %H:%M:%S')
    return int((dt - epoch).total_seconds() // 60)

# Decision points
cases = [
    {'name': 'Case 1: 3/7 18:38', 'sim_min': 9758, 'lat': 22.63, 'lng': 114.25, 'taken_cargo': '342668'},
    {'name': 'Case 2: 3/9 22:31', 'sim_min': 12871, 'lat': 24.09, 'lng': 113.71, 'taken_cargo': '280668'},
    {'name': 'Case 3: 3/28 22:46', 'sim_min': 40246, 'lat': 23.59, 'lng': 116.80, 'taken_cargo': '194654'},
]

# Load all cargos
cargos = []
with open('F:/天池大赛/demo_docs_release_20260529/demo/server/data/cargo_dataset.jsonl', 'r', encoding='utf-8') as f:
    for line in f:
        try:
            d = json.loads(line.strip())
            create = to_sim_min(d['create_time'])
            remove = to_sim_min(d['remove_time'])
            cargos.append({
                'id': d['cargo_id'],
                'create': create,
                'remove': remove,
                'start_lat': float(d['start']['lat']),
                'start_lng': float(d['start']['lng']),
                'end_lat': float(d['end']['lat']),
                'end_lng': float(d['end']['lng']),
                'start_city': d['start']['city'],
                'end_city': d['end']['city'],
                'price': float(d['price']),
                'cost_time': int(d['cost_time_minutes']),
                'load_time': d['load_time'],
                'cargo_name': d['cargo_name'],
            })
        except:
            pass

print(f'Loaded {len(cargos)} cargos')

COST_PER_KM = 1.5

for case in cases:
    sim_min = case['sim_min']
    lat = case['lat']
    lng = case['lng']
    cur_day = sim_min // 1440
    print(f'\n{"="*80}')
    print(f'{case["name"]} (sim_min={sim_min}, day={cur_day}, pos=({lat},{lng}))')
    print(f'{"="*80}')

    # Find the taken cargo
    taken = None
    for c in cargos:
        if c['id'] == case['taken_cargo']:
            taken = c
            break
    if taken:
        dist = haversine_km(lat, lng, taken['start_lat'], taken['start_lng'])
        pickup_min = max(1, math.ceil(dist / 60 * 60))
        load_start = to_sim_min(taken['load_time'][0])
        arrival = sim_min + pickup_min
        load_wait = max(0, load_start - arrival)
        total_est = pickup_min + taken['cost_time'] + load_wait + 30
        finish = sim_min + total_est
        finish_day = finish // 1440
        haul_dist = haversine_km(taken['start_lat'], taken['start_lng'], taken['end_lat'], taken['end_lng'])
        deadhead_cost = dist * COST_PER_KM
        haul_cost = haul_dist * COST_PER_KM
        net = taken['price'] - deadhead_cost - haul_cost
        pph = (net / total_est * 60) if total_est > 0 else 0
        print(f'\n  [ORIGINAL ORDER] cargo={taken["id"]}')
        print(f'  Route: {taken["start_city"]} -> {taken["end_city"]}')
        print(f'  Price={taken["price"]:.0f} | Deadhead={dist:.1f}km | Haul={haul_dist:.1f}km')
        print(f'  Deadhead cost={deadhead_cost:.0f} | Haul cost={haul_cost:.0f} | Net profit={net:.0f}')
        print(f'  Pickup={pickup_min}min | Load wait={load_wait}min | Cost time={taken["cost_time"]}min | Total={total_est}min')
        print(f'  Finish: sim_min={finish} (day {finish_day}) | Next day remaining={1440 - (finish % 1440)}min')
        print(f'  Profit/hour={pph:.0f}')
        if finish_day > cur_day and 1440 - (finish % 1440) < 480:
            print(f'  *** VIOLATION: next day cannot get 480min rest! ***')

    # Find available nearby cargos
    nearby = []
    for c in cargos:
        if c['create'] <= sim_min and c['remove'] >= sim_min:
            dist = haversine_km(lat, lng, c['start_lat'], c['start_lng'])
            if dist <= 300:
                pickup_min = max(1, math.ceil(dist / 60 * 60))
                load_start = to_sim_min(c['load_time'][0])
                load_end = to_sim_min(c['load_time'][1])
                arrival = sim_min + pickup_min
                if arrival > load_end:
                    continue
                load_wait = max(0, load_start - arrival)
                total_est = pickup_min + c['cost_time'] + load_wait + 30
                finish = sim_min + total_est
                finish_day = finish // 1440
                next_remaining = 1440 - (finish % 1440) if finish_day > cur_day else 9999

                haul_dist = haversine_km(c['start_lat'], c['start_lng'], c['end_lat'], c['end_lng'])
                deadhead_cost = dist * COST_PER_KM
                haul_cost = haul_dist * COST_PER_KM
                net = c['price'] - deadhead_cost - haul_cost
                pph = (net / total_est * 60) if total_est > 0 else 0

                nearby.append({
                    'id': c['id'], 'dist': dist, 'pickup_min': pickup_min,
                    'load_wait': load_wait, 'total_est': total_est,
                    'finish': finish, 'finish_day': finish_day,
                    'next_remaining': next_remaining,
                    'price': c['price'], 'net': net, 'pph': pph,
                    'cost_time': c['cost_time'], 'start_city': c['start_city'],
                    'end_city': c['end_city'], 'cargo_name': c['cargo_name'],
                    'load_time': c['load_time'],
                })

    nearby.sort(key=lambda x: x['net'], reverse=True)

    # Categorize
    same_day = [c for c in nearby if c['finish_day'] == cur_day]
    cross_day_safe = [c for c in nearby if c['finish_day'] > cur_day and c['next_remaining'] >= 480]
    cross_day_risky = [c for c in nearby if c['finish_day'] > cur_day and c['next_remaining'] < 480]

    print(f'\n  Nearby cargos: {len(nearby)} total | same_day={len(same_day)} | cross_safe={len(cross_day_safe)} | cross_risky={len(cross_day_risky)}')

    print(f'\n  --- Top 5 by net profit ---')
    for i, c in enumerate(nearby[:5]):
        marker = ' *** ORIGINAL (VIOLATION)' if c['id'] == case['taken_cargo'] else ''
        cat = 'SAME-DAY' if c['finish_day'] == cur_day else ('CROSS-SAFE' if c['next_remaining'] >= 480 else 'CROSS-RISKY')
        print(f'  [{i+1}] {cat} cargo={c["id"]} | {c["start_city"]} -> {c["end_city"]}')
        print(f'      Price={c["price"]:.0f} Net={c["net"]:.0f} PPH={c["pph"]:.0f} | Total={c["total_est"]}min | NextRemain={c["next_remaining"]}min{marker}')

    # Best alternative in each category
    print(f'\n  --- Best alternatives ---')
    if cross_day_safe:
        best = max(cross_day_safe, key=lambda x: x['net'])
        print(f'  CROSS-SAFE best: cargo={best["id"]} | {best["start_city"]}->{best["end_city"]} | Net={best["net"]:.0f} PPH={best["pph"]:.0f} | Total={best["total_est"]}min NextRemain={best["next_remaining"]}min')
    else:
        print(f'  CROSS-SAFE: NONE available')

    if same_day:
        best = max(same_day, key=lambda x: x['net'])
        print(f'  SAME-DAY best: cargo={best["id"]} | {best["start_city"]}->{best["end_city"]} | Net={best["net"]:.0f} PPH={best["pph"]:.0f} | Total={best["total_est"]}min')
    else:
        print(f'  SAME-DAY: NONE available')

    # WAIT strategy estimate
    minutes_to_midnight = 1440 - (sim_min % 1440)
    print(f'\n  --- WAIT option ---')
    print(f'  Minutes to midnight: {minutes_to_midnight}')
    print(f'  Wait until midnight + 480min next day = next decision at sim_min={(cur_day+1)*1440+480}')
    print(f'  Penalty avoided: 2400')
