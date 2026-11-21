"""备用计量器具的调拨排程（纯计算，不依赖数据库）。

输入是校验后的台账、证书、工位需求与调拨路程，输出逐班次的配具去向或未配
原因。排程单位为「班次」：每个需求绑定工位、器具名称、量程、使用区间和停
线优先级。

择优顺序：

1. 先尽量保住更多工位（不同工位计数）；
2. 再按停线优先级择优（数字小者更急，逐级比较覆盖数量）；
3. 再尽量覆盖更多班次；
4. 最后比较调运总耗时。

同一把备用具可在不同班次间复用。器具每次出动都从存放区领出、用毕归还：
上一班次结束后先归还到存放区，再从存放区送往下一工位；归还加再出动的路程
不得晚于下一班次开始。备用具证书须覆盖班次使用全程（签发日不晚于班次开始
日，到期日不早于班次结束日），量程须覆盖需求器具量程。

已确认的调拨是锁定项：先落锁定项，再排其余班次。源数据更新使某锁定失效
时，该锁定标记为冲突并继续占用备用具与该班次，直到释放后才参与重排。
"""

from datetime import datetime, timedelta
import time

from .spec import SPARE

ASSIGNED = '已配'
LOCKED = '已锁定'
CONFLICT = '冲突'
UNASSIGNED = '未配'

NO_SPARE = '无同名备用器具'
RANGE_MISMATCH = '量程不匹配'
CERT_GAP = '证书未覆盖使用全程'
NO_ROUTE = '缺少调拨路程，无法核实来不来得及'
NO_RANGE = '需求器具未登记量程'
HELD = '备用具处于调拨冲突占用，待释放后重排'
BUSY = '备用具均已排满或调运路程不足'

LOCK_CHANGED = '源数据更新后对应工位班次已不存在或已变更'
LOCK_AREA = '源数据更新后工位作业区已变更'
LOCK_SPARE = '备用具已从台账移除或不再为备用'
LOCK_NAME = '备用具名称与需求不一致'
LOCK_RANGE = '备用具量程不满足需求'
LOCK_CERT = '备用具证书未覆盖使用全程'
LOCK_DUPLICATE = '该班次另有已确认调拨，待释放后重排'
LOCK_TRIP = '与同器具已确认调拨的时间或路程冲突'
LOCK_TIME = '班次结束必须晚于开始'

_NODE_BUDGET = 250_000
_TIME_BUDGET = 4.0


def as_datetime(value):
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def parse_travel(rows):
    return {(row['from_area'], row['to_area']): int(row['travel_minutes']) for row in rows}


def choose_certificate(certificates, report_day):
    """签发日不晚于报告日的最新证书，同日按证书编号字典序取末位。"""
    code = max(
        (code for code, (issued, _) in certificates.items() if issued <= report_day),
        key=lambda code: (certificates[code][0], code), default=None,
    )
    return (code, *certificates[code]) if code else None


def build_spares(instruments, certificates, report_day):
    spares = []
    for asset_id, instrument in instruments.items():
        if instrument['asset_status'] != SPARE:
            continue
        cert = choose_certificate(certificates.get(asset_id, {}), report_day)
        spares.append({
            'asset_id': asset_id,
            'asset_name': instrument['asset_name'],
            'work_area': instrument['work_area'],
            'range_min': instrument.get('range_min'),
            'range_max': instrument.get('range_max'),
            'certificate_id': cert[0] if cert else None,
            'issued_on': cert[1] if cert else None,
            'expires_on': cert[2] if cert else None,
        })
    spares.sort(key=lambda item: item['asset_id'])
    return spares


def build_slots(demands, instruments, report_day):
    slots = []
    for demand in demands:
        start, end = as_datetime(demand['shift_start']), as_datetime(demand['shift_end'])
        if start.date() != report_day:
            continue
        instrument = instruments.get(demand['asset_id'], {})
        slots.append({
            'station_id': demand['station_id'],
            'work_area': demand.get('work_area') or instrument.get('work_area') or '',
            'asset_id': demand['asset_id'],
            'asset_name': instrument.get('asset_name') or '',
            'range_min': instrument.get('range_min'),
            'range_max': instrument.get('range_max'),
            'start': start,
            'end': end,
            'priority': demand.get('stop_priority', 5),
        })
    slots.sort(key=lambda slot: (slot['start'], slot['priority'], slot['station_id'], slot['asset_id']))
    for index, slot in enumerate(slots):
        slot['key'] = index
    return slots


def plan(slots, spares, travel, instruments, locks=None):
    """排定各班次的备用具，并复核报告范围内的全部生效锁定。"""
    slots = [dict(slot) for slot in slots]
    slots.sort(key=lambda slot: (slot['start'], slot['priority'], slot['station_id'], slot['asset_id']))
    for index, slot in enumerate(slots):
        slot['key'] = index
    spare_by_id = {spare['asset_id']: spare for spare in spares}
    by_slot = {slot_key(slot): slot for slot in slots}
    if slots:
        low, high = min(slot['start'] for slot in slots), max(slot['end'] for slot in slots)
        locks = [lock for lock in (locks or [])
                 if as_datetime(lock['shift_start']) <= high and as_datetime(lock['shift_end']) >= low]
    else:
        locks = []
    locks.sort(key=lambda item: (item.get('id') is None, item.get('id') or 0))

    evaluations = []
    for lock in locks:
        slot = by_slot.get(lock_key(lock))
        evaluations.append({'lock': lock, 'slot': slot,
                            'reason': _lock_reason(lock, slot, spare_by_id, instruments)})

    claims = {}
    for evaluation in evaluations:
        if evaluation['slot'] is not None:
            key = evaluation['slot']['key']
            claims[key] = claims.get(key, 0) + 1
    for evaluation in evaluations:
        if evaluation['slot'] is not None and claims[evaluation['slot']['key']] > 1:
            evaluation['reason'] = evaluation['reason'] or LOCK_DUPLICATE

    # 逐条试落锁定，检测同一备用具上锁定之间的时间/路程冲突。
    fixed = {spare['asset_id']: [] for spare in spares}
    for evaluation in evaluations:
        if evaluation['reason'] is not None:
            continue
        lock, slot = evaluation['lock'], evaluation['slot']
        spare = spare_by_id[lock['spare_id']]
        interval = _interval(slot)
        if not fits(fixed[spare['asset_id']], interval, travel, spare['work_area']):
            evaluation['reason'] = LOCK_TRIP
        elif route_total(spare['work_area'], fixed[spare['asset_id']] + [interval], travel) is None:
            evaluation['reason'] = NO_ROUTE
        else:
            fixed[spare['asset_id']].append(interval)

    # 冲突锁定「继续占用」：整个备用具在释放前不再参与新排程。
    held = {evaluation['lock']['spare_id'] for evaluation in evaluations
            if evaluation['reason'] is not None} & set(spare_by_id)
    for evaluation in evaluations:
        if evaluation['reason'] is None and evaluation['lock']['spare_id'] in held:
            evaluation['reason'] = HELD

    # 依最终判定的有效锁定重建占用集，避免冲突器具残留旧区间。
    fixed = {spare['asset_id']: [] for spare in spares}
    for evaluation in evaluations:
        if evaluation['reason'] is None:
            fixed[evaluation['lock']['spare_id']].append(_interval(evaluation['slot']))

    covered, slot_locks, conflict_slots = {}, {}, {}
    for evaluation in evaluations:
        slot = evaluation['slot']
        if slot is None:
            continue
        if evaluation['reason'] is None:
            covered[slot['key']] = evaluation['lock']['spare_id']
            slot_locks[slot['key']] = evaluation['lock']
        else:
            conflict_slots[slot['key']] = evaluation

    open_slots = [slot for slot in slots
                  if slot['key'] not in covered and slot['key'] not in conflict_slots]
    candidates = {}
    for slot in open_slots:
        pool = []
        for spare in spares:
            if spare['asset_id'] in held or not _matches(spare, slot):
                continue
            if route_total(spare['work_area'], fixed[spare['asset_id']] + [_interval(slot)], travel) is not None:
                pool.append(spare['asset_id'])
        pool.sort(key=lambda spare_id: (_outbound(travel, spare_by_id[spare_id]['work_area'], slot['work_area']),
                                        spare_id))
        candidates[slot['key']] = pool

    assignment = _optimize(open_slots, fixed, candidates, travel, spare_by_id)
    for key, spare_id in assignment.items():
        fixed[spare_id].append(_interval(_slot_by_key(open_slots, key)))
        covered[key] = spare_id

    rows = _slot_rows(slots, covered, conflict_slots, slot_locks, spare_by_id, fixed, travel, held)
    usage = _usage_rows(spares, fixed, travel, held)
    views = [_lock_view(evaluation) for evaluation in evaluations]
    return {
        'rows': rows,
        'usage': usage,
        'locks': views,
        'metrics': {
            'slots': len(slots),
            'stations': len({slot['station_id'] for slot in slots}),
            'assigned': len(covered),
            'locked': sum(1 for view in views if view['state'] == 'locked'),
            'conflicts': sum(1 for view in views if view['state'] == 'conflict'),
            'unassigned': sum(1 for row in rows if row['status'] == UNASSIGNED),
            'travel_minutes': sum(entry['travel_minutes'] or 0 for entry in usage),
        },
    }


def validate_transfer(lock, instruments, certificates, travel, existing, report_day):
    """确认调拨前的整体核验；返回未通过原因或 None。"""
    start, end = as_datetime(lock['shift_start']), as_datetime(lock['shift_end'])
    if end <= start:
        return LOCK_TIME
    instrument = instruments.get(lock['demand_asset_id'])
    spare_instrument = instruments.get(lock['spare_id'])
    if instrument is None or spare_instrument is None:
        return LOCK_CHANGED
    if spare_instrument['asset_status'] != SPARE:
        return LOCK_SPARE
    cert = choose_certificate(certificates.get(lock['spare_id'], {}), report_day)
    spare = {
        'asset_name': spare_instrument['asset_name'],
        'work_area': spare_instrument['work_area'],
        'range_min': spare_instrument.get('range_min'),
        'range_max': spare_instrument.get('range_max'),
        'issued_on': cert[1] if cert else None,
        'expires_on': cert[2] if cert else None,
    }
    if spare['asset_name'] != instrument['asset_name']:
        return LOCK_NAME
    if not _range_covers(spare, instrument):
        return LOCK_RANGE
    if not _cert_covers(spare, start, end):
        return LOCK_CERT
    intervals = [{'start': as_datetime(item['shift_start']), 'end': as_datetime(item['shift_end']),
                  'work_area': item['work_area']} for item in existing
                 if str(item.get('id')) != str(lock.get('id')) and item['spare_id'] == lock['spare_id']]
    candidate = {'start': start, 'end': end, 'work_area': lock['work_area']}
    if not fits(intervals, candidate, travel, spare['work_area']):
        return LOCK_TRIP
    if route_total(spare['work_area'], intervals + [candidate], travel) is None:
        return NO_ROUTE
    return None


def fits(intervals, candidate, travel, storage):
    """上一班次结束 → 归还存放区 → 下一工位，是否赶得上下一班次开始。"""
    ordered = sorted(list(intervals) + [candidate], key=lambda item: (item['start'], item['end']))
    for left, right in zip(ordered, ordered[1:]):
        gap = _return_trip(travel, storage, left['work_area'], right['work_area'])
        if gap is None or left['end'] + timedelta(minutes=gap) > right['start']:
            return False
    return True


def route_total(storage, intervals, travel):
    """调运总路程：存放区 → 首个工位，之后每次「归还存放区 → 下一工位」。

    末班后不再计入返库，因为规划窗口内已无后续出动。缺路段返回 None。
    """
    total = 0
    previous = storage
    for item in sorted(intervals, key=lambda entry: (entry['start'], entry['end'])):
        if previous == storage:
            leg = _leg(travel, storage, item['work_area'])
        else:
            back = _leg(travel, previous, storage)
            out = _leg(travel, storage, item['work_area'])
            leg = None if back is None or out is None else back + out
        if leg is None:
            return None
        total += leg
        previous = item['work_area']
    return total


def slot_key(slot):
    return (slot['station_id'], slot['asset_id'], slot['start'], slot['end'])


def lock_key(lock):
    return (lock['station_id'], lock['demand_asset_id'],
            as_datetime(lock['shift_start']), as_datetime(lock['shift_end']))


def _optimize(open_slots, fixed, candidates, travel, spare_by_id):
    if not open_slots:
        return {}
    deadline = time.monotonic() + _TIME_BUDGET
    slots_by_key = {slot['key']: slot for slot in open_slots}
    storage = {spare_id: spare['work_area'] for spare_id, spare in spare_by_id.items()}
    intervals = {spare_id: list(items) for spare_id, items in fixed.items()}
    base_stations = {item['station_id'] for items in fixed.values() for item in items}
    base_priority = _priority_vector(item for items in fixed.values() for item in items)
    base_count = sum(len(items) for items in fixed.values())
    best = {'score': None, 'assignment': {}}
    budget = [_NODE_BUDGET]
    # 候选少的需求先搜，便于尽早剪枝；择优结果与搜索顺序无关。
    order = sorted(range(len(open_slots)),
                   key=lambda i: (len(candidates.get(open_slots[i]['key'], [])),
                                  open_slots[i]['start'], open_slots[i]['priority']))

    def score(assignment):
        picked = [slots_by_key[key] for key in assignment]
        stations = base_stations | {slot['station_id'] for slot in picked}
        priority = list(base_priority)
        for slot in picked:
            priority[slot['priority'] - 1] += 1
        journeys = 0
        for spare_id, items in intervals.items():
            legs = route_total(storage[spare_id], items, travel)
            if legs is None:
                return None
            journeys += legs
        return (len(stations), tuple(priority), base_count + len(picked), -journeys)

    def attempt(spare_id, slot, local):
        candidate = _interval(slot)
        if not fits(local[spare_id], candidate, travel, storage[spare_id]):
            return False
        if route_total(storage[spare_id], local[spare_id] + [candidate], travel) is None:
            return False
        local[spare_id].append(candidate)
        return True

    def seed():
        local = {spare_id: list(items) for spare_id, items in intervals.items()}
        picked = {}
        for index in order:
            slot = open_slots[index]
            for spare_id in candidates.get(slot['key'], []):
                if attempt(spare_id, slot, local):
                    picked[slot['key']] = spare_id
                    break
        return picked

    picked = seed()
    for spare_id, items in intervals.items():
        intervals[spare_id] = list(fixed[spare_id])
    for key, spare_id in picked.items():
        intervals[spare_id].append(_interval(slots_by_key[key]))
    initial = score(set(picked))
    if initial is not None:
        best['score'], best['assignment'] = initial, dict(picked)
    for spare_id, items in intervals.items():
        intervals[spare_id] = list(fixed[spare_id])

    remaining_stations = [set() for _ in range(len(order) + 1)]
    for position in range(len(order) - 1, -1, -1):
        remaining_stations[position] = remaining_stations[position + 1] | {
            open_slots[order[position]]['station_id']}

    def walk(depth, assignment):
        budget[0] -= 1
        if budget[0] <= 0 or (budget[0] % 512 == 0 and time.monotonic() > deadline):
            return
        if depth == len(order):
            value = score(set(assignment))
            if value is not None and (best['score'] is None or value > best['score']):
                best['score'], best['assignment'] = value, dict(assignment)
            return
        if best['score'] is not None:
            stations = base_stations | {slots_by_key[key]['station_id'] for key in assignment} | \
                remaining_stations[depth]
            reachable = base_count + len(assignment) + (len(order) - depth)
            if len(stations) < best['score'][0]:
                return
            if len(stations) == best['score'][0] and reachable < best['score'][2]:
                return
        slot = open_slots[order[depth]]
        for spare_id in candidates.get(slot['key'], []):
            candidate = _interval(slot)
            if attempt(spare_id, slot, intervals):
                assignment[slot['key']] = spare_id
                walk(depth + 1, assignment)
                del assignment[slot['key']]
                intervals[spare_id].pop()
        walk(depth + 1, assignment)

    walk(0, {})
    return best['assignment']


def _priority_vector(slots):
    vector = [0, 0, 0, 0, 0]
    for slot in slots:
        vector[max(0, min(4, slot['priority'] - 1))] += 1
    return vector


def _increments(spare_id, storage, items, travel):
    """每个班次对应的调拨路程：首班为出库 → 工位；其后为归还 → 再出库 → 工位。"""
    increments = {}
    previous = storage
    for item in sorted(items, key=lambda entry: (entry['start'], entry['end'])):
        if previous == storage:
            leg = _leg(travel, storage, item['work_area'])
        else:
            back = _leg(travel, previous, storage)
            out = _leg(travel, storage, item['work_area'])
            leg = None if back is None or out is None else back + out
        increments[(spare_id, item['slot'])] = leg
        previous = item['work_area']
    return increments


def _slot_rows(slots, covered, conflict_slots, slot_locks, spare_by_id, fixed, travel, held):
    increments = {}
    for spare_id, items in fixed.items():
        increments.update(_increments(spare_id, spare_by_id[spare_id]['work_area'], items, travel))
    rows = []
    for slot in slots:
        spare_id = covered.get(slot['key'])
        spare = spare_by_id.get(spare_id) if spare_id else None
        lock = slot_locks.get(slot['key'])
        conflict = conflict_slots.get(slot['key'])
        held_spare = spare_by_id.get(conflict['lock']['spare_id']) if conflict else None
        shown = spare or held_spare
        row = {
            'key': slot['key'],
            'station_id': slot['station_id'],
            'work_area': slot['work_area'],
            'asset_id': slot['asset_id'],
            'asset_name': slot['asset_name'],
            'range': _range_text(slot['range_min'], slot['range_max']),
            'shift': f"{slot['start']:%Y-%m-%d %H:%M}–{slot['end']:%H:%M}",
            'shift_start': slot['start'].strftime('%Y-%m-%dT%H:%M'),
            'shift_end': slot['end'].strftime('%Y-%m-%dT%H:%M'),
            'priority': slot['priority'],
            'spare_id': (shown or {}).get('asset_id', '—'),
            'spare_location': (shown or {}).get('work_area', '—'),
            'certificate_id': (shown or {}).get('certificate_id') or '—',
            'travel_minutes': increments.get((spare_id, slot['key'])) if spare else '—',
            'status': UNASSIGNED,
            'note': '',
            'lock_id': (lock or (conflict['lock'] if conflict else None) or {}).get('id'),
            'lock_state': 'locked' if lock else ('conflict' if conflict else None),
        }
        if spare:
            row['status'] = LOCKED if lock else ASSIGNED
            row['note'] = f"调拨至 {slot['station_id']}（{slot['work_area']}）"
        elif conflict:
            row['status'] = CONFLICT
            row['note'] = conflict['reason']
        else:
            row['note'] = _explain(slot, spare_by_id, travel, held)
        rows.append(row)
    return rows


def _explain(slot, spare_by_id, travel, held):
    same = [spare for spare in spare_by_id.values() if spare['asset_name'] == slot['asset_name']]
    if not same:
        return NO_SPARE
    if slot['range_min'] is None or slot['range_max'] is None:
        return NO_RANGE
    ranged = [spare for spare in same if _range_covers(spare, slot)]
    if not ranged:
        detail = '、'.join(f"{spare['asset_id']} {_range_text(spare['range_min'], spare['range_max'])}"
                           for spare in same)
        return f'{RANGE_MISMATCH}（现有 {detail}）'
    certed = [spare for spare in ranged if _cert_covers(spare, slot['start'], slot['end'])]
    if not certed:
        detail = '、'.join(f"{spare['asset_id']} 证书至 {spare['expires_on']}" for spare in ranged)
        return f'{CERT_GAP}（{detail}）'
    routable = [spare for spare in certed
                if _outbound(travel, spare['work_area'], slot['work_area']) is not None]
    if not routable:
        return NO_ROUTE
    blocked = [spare['asset_id'] for spare in routable if spare['asset_id'] in held]
    if blocked and len(blocked) == len(routable):
        return f'{HELD}（{"、".join(blocked)}）'
    return BUSY


def _range_covers(spare, target):
    low, high = spare.get('range_min'), spare.get('range_max')
    need_low, need_high = target.get('range_min'), target.get('range_max')
    if None in (low, high, need_low, need_high):
        return False
    return low <= need_low and high >= need_high


def _cert_covers(spare, start, end):
    issued, expires = spare.get('issued_on'), spare.get('expires_on')
    if issued is None or expires is None:
        return False
    return issued <= start.date() and expires >= end.date()


def _matches(spare, slot):
    return spare['asset_name'] == slot['asset_name'] and _range_covers(spare, slot) \
        and _cert_covers(spare, slot['start'], slot['end'])


def _interval(slot):
    return {'start': slot['start'], 'end': slot['end'], 'work_area': slot['work_area'],
            'slot': slot['key'], 'station_id': slot['station_id'], 'priority': slot['priority']}


def _slot_by_key(slots, key):
    for slot in slots:
        if slot['key'] == key:
            return slot
    raise KeyError(key)


def _leg(travel, origin, target):
    if origin == target:
        return 0
    if (origin, target) in travel:
        return travel[(origin, target)]
    if (target, origin) in travel:
        return travel[(target, origin)]
    return None


def _outbound(travel, storage, target):
    return _leg(travel, storage, target)


def _return_trip(travel, storage, origin, target):
    """上一工位归还存放区，再从存放区到下一工位。"""
    back, out = _leg(travel, origin, storage), _leg(travel, storage, target)
    if back is None or out is None:
        return None
    return back + out


def _range_text(low, high):
    return f'{_number(low)}–{_number(high)}' if low is not None and high is not None else '未登记'


def _number(value):
    if value is None:
        return '—'
    return int(value) if float(value).is_integer() else value


def _usage_rows(spares, fixed, travel, held):
    rows = []
    for spare in spares:
        items = sorted(fixed.get(spare['asset_id'], []), key=lambda entry: (entry['start'], entry['end']))
        if not items:
            continue
        rows.append({
            'asset_id': spare['asset_id'],
            'asset_name': spare['asset_name'],
            'work_area': spare['work_area'],
            'certificate_id': spare.get('certificate_id') or '—',
            'expires_on': spare['expires_on'].isoformat() if spare.get('expires_on') else '—',
            'served': '；'.join(f"{item['station_id']} {item['start']:%m-%d %H:%M}–{item['end']:%H:%M}"
                               for item in items),
            'travel_minutes': route_total(spare['work_area'], items, travel),
            'state': '冲突占用' if spare['asset_id'] in held else '可用',
        })
    return rows


def _lock_reason(lock, slot, spare_by_id, instruments):
    spare = spare_by_id.get(lock['spare_id'])
    if spare is None:
        return LOCK_SPARE
    instrument = instruments.get(lock['demand_asset_id'])
    if instrument is None or slot is None:
        return LOCK_CHANGED
    if lock.get('work_area') and lock['work_area'] != slot['work_area']:
        return LOCK_AREA
    if spare['asset_name'] != instrument['asset_name']:
        return LOCK_NAME
    if not _range_covers(spare, instrument):
        return LOCK_RANGE
    if not _cert_covers(spare, slot['start'], slot['end']):
        return LOCK_CERT
    return None


def _lock_view(evaluation):
    lock = evaluation['lock']
    return {
        'id': lock.get('id'),
        'station_id': lock['station_id'],
        'demand_asset_id': lock['demand_asset_id'],
        'spare_id': lock['spare_id'],
        'work_area': lock.get('work_area'),
        'shift_start': _iso(lock['shift_start']),
        'shift_end': _iso(lock['shift_end']),
        'state': 'conflict' if evaluation['reason'] else 'locked',
        'reason': evaluation['reason'],
    }


def _iso(value):
    return as_datetime(value).strftime('%Y-%m-%dT%H:%M')
