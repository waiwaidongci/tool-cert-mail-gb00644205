"""校准证书的报告日快照、器具处置清单、备用具调拨与分发判定。"""

from collections import Counter, defaultdict
from datetime import date, datetime

from . import dispatch
from .spec import ACTIVE, CERTIFICATE, DEMAND, INACTIVE, INSTRUMENT, SPEC, SPARE, TRAVEL

OVERDUE = '已过期'
MISSING = '缺少证书'
DUE_TODAY = '当日到期'
DUE_SOON = '即将到期'
VALID = '有效'
STATUS_ORDER = (OVERDUE, MISSING, DUE_TODAY, DUE_SOON, VALID)

_FIELDS = {field['key']: field for field in SPEC['fields']}
_COLUMNS = (
    ('asset_id', '器具编号'), ('asset_name', '器具名称'), ('work_area', '作业区'),
    ('owner', '责任班组'), ('status', '证书状态'), ('certificate_id', '证书编号'),
    ('issued_on', '签发日'), ('expires_on', '到期日'), ('days_remaining', '剩余天数'),
    ('action', '班组处置'),
)
_DISPATCH_COLUMNS = (
    ('station_id', '工位'), ('work_area', '作业区'), ('asset_id', '需求器具'),
    ('asset_name', '器具名称'), ('range', '需求量程'), ('shift', '班次'),
    ('priority', '停线优先级'), ('status', '配具状态'), ('spare_id', '备用具'),
    ('spare_location', '备用具存放区'), ('certificate_id', '备用具证书'),
    ('travel_minutes', '出动路程（分钟）'), ('note', '去向或未配原因'),
)
_USAGE_COLUMNS = (
    ('asset_id', '备用具'), ('asset_name', '器具名称'), ('work_area', '存放区'),
    ('certificate_id', '证书编号'), ('expires_on', '证书到期日'), ('state', '占用状态'),
    ('served', '承担班次'), ('travel_minutes', '调运总路程（分钟）'),
)
_INSTRUMENT_KEYS = ('asset_name', 'work_area', 'owner', 'asset_status', 'range_min', 'range_max')
_CERTIFICATE_KEYS = ('certificate_id', 'issued_on', 'expires_on')
_DEMAND_KEYS = ('station_id', 'shift_start', 'shift_end', 'stop_priority')
_TRAVEL_KEYS = ('from_area', 'to_area', 'travel_minutes')
_ALLOWED = {
    INSTRUMENT: ('record_type', 'asset_id') + _INSTRUMENT_KEYS,
    CERTIFICATE: ('record_type', 'asset_id') + _CERTIFICATE_KEYS,
    DEMAND: ('record_type', 'asset_id', 'work_area') + _DEMAND_KEYS,
    TRAVEL: ('record_type',) + _TRAVEL_KEYS,
}


def _text(row, key):
    value = row.get(key)
    field = _FIELDS[key]
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= field['max_length']:
        raise ValueError(f'{field["label"]}须填写 1 至 {field["max_length"]} 个字符')
    return value.strip()


def _day(value, label):
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            pass
        else:
            if parsed.isoformat() == value:
                return parsed
    raise ValueError(f'{label}须为有效的 YYYY-MM-DD 日期')


def _moment(value, label):
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            pass
        else:
            if parsed.second == 0 and parsed.microsecond == 0:
                return parsed
    raise ValueError(f'{label}须为有效的 YYYY-MM-DD HH:MM 时刻')


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{label}须为数字')
    return value


def _integer(value, label, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{label}须为 {low} 至 {high} 的整数')
    return value


def _blank(row, kind):
    allowed = _ALLOWED[kind]
    filled = [key for key in _FIELDS if key not in allowed and row.get(key) not in (None, '')]
    if filled:
        raise ValueError(f'{kind}不应填写 {_FIELDS[filled[0]]["label"]}')


def validate_rows(rows):
    instruments = {}
    certificates = defaultdict(dict)
    demands = []
    travel = []
    for index, row in enumerate(rows, start=2):
        try:
            kind = row.get('record_type')
            if kind not in _ALLOWED:
                raise ValueError('记录类型须为器具台账、校准证书、工位需求或调拨路程')
            _blank(row, kind)
            if kind == TRAVEL:
                travel.append({'from_area': _text(row, 'from_area'), 'to_area': _text(row, 'to_area'),
                               'travel_minutes': _integer(row.get('travel_minutes'), '调拨路程', 0, 1440)})
                continue
            asset_id = _text(row, 'asset_id')
            if kind == INSTRUMENT:
                if asset_id in instruments:
                    raise ValueError(f'器具 {asset_id} 的台账重复')
                status = row.get('asset_status')
                if status not in (ACTIVE, INACTIVE, SPARE):
                    raise ValueError('器具状态须为在用、停用或备用')
                low, high = row.get('range_min'), row.get('range_max')
                if (low is None) != (high is None):
                    raise ValueError('量程上下限须同时填写或同时留空')
                if low is not None:
                    low = _number(low, '量程下限')
                    high = _number(high, '量程上限')
                    if high < low:
                        raise ValueError('量程上限不得小于下限')
                if status == SPARE and low is None:
                    raise ValueError('备用器具须登记量程，才能核验量程是否匹配')
                instruments[asset_id] = {
                    'asset_id': asset_id, 'asset_status': status, 'range_min': low, 'range_max': high,
                    **{key: _text(row, key) for key in ('asset_name', 'work_area', 'owner')},
                }
            elif kind == CERTIFICATE:
                code = _text(row, 'certificate_id')
                issued = _day(row.get('issued_on'), '签发日')
                expires = _day(row.get('expires_on'), '到期日')
                if expires < issued:
                    raise ValueError(f'证书 {code} 的到期日早于签发日')
                if code in certificates[asset_id]:
                    raise ValueError(f'器具 {asset_id} 的证书 {code} 重复')
                certificates[asset_id][code] = (issued, expires)
            elif kind == DEMAND:
                start = _moment(row.get('shift_start'), '班次开始')
                end = _moment(row.get('shift_end'), '班次结束')
                if end <= start:
                    raise ValueError('班次结束须晚于班次开始')
                raw_priority = row.get('stop_priority')
                priority = 5 if raw_priority is None else _integer(raw_priority, '停线优先级', 1, 5)
                demands.append({'station_id': _text(row, 'station_id'), 'asset_id': asset_id,
                                'work_area': row.get('work_area') or None,
                                'shift_start': start.isoformat(), 'shift_end': end.isoformat(),
                                'stop_priority': priority})
        except ValueError as error:
            raise ValueError(f'CSV 第 {index} 行 · {error}') from None

    unknown = sorted(certificates.keys() - instruments.keys())
    if unknown:
        raise ValueError(f'证书缺少对应的器具台账，例如 {unknown[0]}')
    areas = {item['work_area'] for item in instruments.values()}
    demand_keys = set()
    for demand in demands:
        if demand['asset_id'] not in instruments:
            raise ValueError(f'工位需求缺少对应的器具台账，例如 {demand["asset_id"]}')
        area = demand['work_area'] or instruments[demand['asset_id']]['work_area']
        if area not in areas:
            raise ValueError(f'工位需求引用了未知作业区 {area}')
        key = (demand['station_id'], demand['asset_id'], demand['shift_start'], demand['shift_end'])
        if key in demand_keys:
            raise ValueError(f'工位 {demand["station_id"]} 的同一班次需求重复')
        demand_keys.add(key)
    travel_keys = set()
    for section in travel:
        key = (section['from_area'], section['to_area'])
        if key in travel_keys:
            raise ValueError(f"调拨路程 {section['from_area']} → {section['to_area']} 重复")
        travel_keys.add(key)
        for area in key:
            if area not in areas:
                raise ValueError(f'调拨路程引用了未知作业区 {area}')
    return instruments, certificates, demands, travel


def _parameters(params):
    values = {}
    for field in SPEC['params']:
        key = field['key']
        value = params.get(key, field['default'])
        if field['type'] == 'date':
            value = _day(value, field['label'])
        elif type(value) is not int or not field['min'] <= value <= field['max']:
            raise ValueError(f'{field["label"]}须为 {field["min"]} 至 {field["max"]} 的整数')
        values[key] = value
    return values


def _asset_report(instrument, certificates, report_day, horizon):
    code = dispatch.choose_certificate(certificates, report_day)
    code = code[0] if code else None
    owner = instrument['owner']
    issued = expires = remaining = None
    if code is None:
        status = MISSING
        action = f'{owner}核对器具铭牌并向计量室补录证书。确认校准有效前暂停用于检验。'
    else:
        issued, expires = certificates[code]
        remaining = (expires - report_day).days
        if remaining < 0:
            status = OVERDUE
            action = f'{owner}停用并贴隔离标签，换用有效备用器具，登记送校。'
        elif remaining == 0:
            status = DUE_TODAY
            action = f'{owner}本班交接前安排换具和送校，次日启用前核对新证书。'
        elif remaining <= horizon:
            status = DUE_SOON
            action = f'{owner}在 {expires} 前预约送校并落实有效备用器具。'
        else:
            status = VALID
            action = f'{owner}继续使用，按台账日期复核校准计划。'

    return {
        **instrument, 'status': status, 'certificate_id': code,
        'issued_on': issued.isoformat() if issued else None,
        'expires_on': expires.isoformat() if expires else None,
        'days_remaining': remaining, 'action': action,
    }


def analyze(rows, params, locks=None) -> dict:
    options = _parameters(params)
    day = options['report_day']
    horizon = options['horizon_days']
    threshold = options['min_coverage_percent']
    instruments, certificates, demands, travel_rows = validate_rows(rows)
    snapshot = [
        _asset_report(instrument, certificates.get(asset_id, {}), day, horizon)
        for asset_id, instrument in instruments.items() if instrument['asset_status'] == ACTIVE
    ]
    snapshot.sort(key=lambda row: (STATUS_ORDER.index(row['status']), row['days_remaining'] or 0, row['asset_id']))
    counts = Counter(row['status'] for row in snapshot)
    total = len(snapshot)
    covered = total - counts[MISSING]
    coverage = f'{covered / total:.2%}' if total else '待核实'
    ready = total > 0 and covered * 100 >= threshold * total

    spares = dispatch.build_spares(instruments, certificates, day)
    slots = dispatch.build_slots(demands, instruments, day)
    travel = dispatch.parse_travel(travel_rows)
    plan = dispatch.plan(slots, spares, travel, instruments, locks)
    dispatch_ready = plan['metrics']['slots'] > 0 and plan['metrics']['unassigned'] == 0 \
        and plan['metrics']['conflicts'] == 0

    if not ready:
        headline = (f'{day} 班报暂停分发 · 已覆盖 {covered}/{total} 台，低于 {threshold}% 门槛'
                    if total else f'{day} 班报暂停分发 · 台账无在用器具')
    else:
        headline = f'{day} 班报可分发 · {covered}/{total} 台有证书，{counts[OVERDUE]} 台已过期'
        if plan['metrics']['slots']:
            if plan['metrics']['unassigned']:
                headline += f' · {plan["metrics"]["unassigned"]} 个班次未配到备用具'
            elif plan['metrics']['conflicts']:
                headline += f' · {plan["metrics"]["conflicts"]} 条已确认调拨与源数据冲突'

    metrics = [
        {'label': '在用器具', 'value': total, 'detail': '按导入台账的在用状态统计'},
        {'label': '证书覆盖率', 'value': coverage, 'detail': f'{covered}/{total} 台 · 门槛 {threshold}%'},
        {'label': '已过期', 'value': counts[OVERDUE], 'detail': '立即停用并换具'},
        {'label': '临期（含当日）', 'value': counts[DUE_SOON] + counts[DUE_TODAY],
         'detail': f'其中 {counts[DUE_TODAY]} 台当日到期'},
        {'label': '缺少证书', 'value': counts[MISSING], 'detail': '截至报告日无已签发证书'},
    ]
    if plan['metrics']['slots']:
        metrics.extend([
            {'label': '待配班次', 'value': plan['metrics']['slots'],
             'detail': f'覆盖 {plan["metrics"]["stations"]} 个工位'},
            {'label': '已配班次', 'value': plan['metrics']['assigned'],
             'detail': f'含已确认锁定 {plan["metrics"]["locked"]} 条'},
            {'label': '未配班次', 'value': plan['metrics']['unassigned'], 'detail': '见逐班次去向或未配原因'},
            {'label': '调拨冲突', 'value': plan['metrics']['conflicts'], 'detail': '已确认调拨待释放后重排'},
            {'label': '调运总耗时', 'value': f'{plan["metrics"]["travel_minutes"]} 分钟',
             'detail': '按出动领取、用毕归还累计'},
        ])

    tables = [{
        'title': '在用器具证书与班组处置',
        'columns': [{'key': key, 'label': label} for key, label in _COLUMNS],
        'rows': snapshot,
    }]
    if plan['rows']:
        tables.append({
            'title': '备用具调拨与逐班次去向',
            'columns': [{'key': key, 'label': label} for key, label in _DISPATCH_COLUMNS],
            'rows': plan['rows'],
        })
    if plan['usage']:
        tables.append({
            'title': '备用具运力与调运耗时',
            'columns': [{'key': key, 'label': label} for key, label in _USAGE_COLUMNS],
            'rows': plan['usage'],
        })
    charts = [{
        'title': '证书状态（器具数）', 'kind': 'bar',
        'series': [{'label': status, 'value': counts[status]} for status in STATUS_ORDER],
    }] if total else []
    if plan['rows']:
        summary = Counter(row['status'] for row in plan['rows'])
        charts.append({
            'title': '配具状态（班次）', 'kind': 'bar',
            'series': [{'label': status, 'value': summary.get(status, 0)}
                       for status in (dispatch.LOCKED, dispatch.ASSIGNED, dispatch.CONFLICT, dispatch.UNASSIGNED)],
        })

    return {
        'ready': ready,
        'dispatch_ready': dispatch_ready,
        'headline': headline,
        'note': f'报告日 {day}，临期窗口 {horizon} 天。证书覆盖率 {coverage}，分发门槛 {threshold}%。'
                '已过期证书计入覆盖。同日签发按证书编号取字典序末位。'
                '备用具按「保住工位 → 停线优先级 → 调运总耗时」择优，含归还后到下一工位的路程。',
        'metrics': metrics,
        'tables': tables,
        'charts': charts,
        'dispatch': {
            'metrics': plan['metrics'],
            'locks': plan['locks'],
            'rows': plan['rows'],
        },
    }
