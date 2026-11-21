"""已确认调拨的持久化、并发确认、冲突复核与释放。"""

import threading
from contextlib import contextmanager

from sqlalchemy import select, text

from src.domain import dispatch
from src.domain.analysis import validate_rows
from src.models.jobs import Transfer
from src.models.source import Dataset
from src.services.audit import record
from src.utils.time import now

# 同一进程内串行化确认与重算；跨进程（后端与 worker 分属不同容器）由 MySQL
# 命名锁串行化；单实例部署下线程锁即足够。
_CONFIRM_LOCK = threading.Lock()
_GUARD_NAME = 'tool-cert-mail-dispatch'


@contextmanager
def guard(db):
    """确认与重算的互斥闸门，保证同一把备用具不会同时被排给赶不上的两站。"""
    if db.get_bind().dialect.name == 'mysql':
        got = db.scalar(text('SELECT GET_LOCK(:name, :timeout)'),
                        {'name': _GUARD_NAME, 'timeout': 5})
        try:
            yield got == 1
        finally:
            if got == 1:
                db.execute(text('SELECT RELEASE_LOCK(:name)'), {'name': _GUARD_NAME})
        return
    with _CONFIRM_LOCK:
        yield True


def serialize(item):
    return {
        'id': item.id,
        'station_id': item.station_id,
        'work_area': item.work_area,
        'demand_asset_id': item.demand_asset_id,
        'spare_id': item.spare_id,
        'shift_start': item.shift_start.strftime('%Y-%m-%dT%H:%M'),
        'shift_end': item.shift_end.strftime('%Y-%m-%dT%H:%M'),
        'dataset_id': item.dataset_id,
        'state': item.state,
        'reason': item.reason,
        'author': item.author,
        'confirmed_at': item.confirmed_at,
        'released_at': item.released_at,
        'released_by': item.released_by,
        'release_note': item.release_note,
    }


def parse(rows):
    return validate_rows(rows)


def confirm(db, body, actor):
    """确认一条调拨，锁定该备用具。

    确认与重算经同一把闸门串行化，且逐条复算与已生效调拨的时间与路程，
    确保同一把备用具不会被排给赶不上的两个班次。
    """
    dataset = db.get(Dataset, body.dataset_id)
    if dataset is None:
        raise ValueError('找不到指定的数据批次')
    with guard(db) as acquired:
        if not acquired:
            raise ValueError('调拨正在并发处理，请稍后重试')
        instruments, certificates, _, travel_rows = parse(dataset.rows)
        travel = dispatch.parse_travel(travel_rows)
        if body.demand_asset_id not in instruments:
            raise ValueError('需求器具不在该批次台账中')
        if body.spare_id not in instruments:
            raise ValueError('备用具不在该批次台账中')
        report_day = body.report_day or body.shift_start.date()
        existing = list(db.scalars(select(Transfer)
                                   .where(Transfer.spare_id == body.spare_id,
                                          Transfer.released_at.is_(None))
                                   .with_for_update()))
        candidate = {
            'id': None, 'station_id': body.station_id,
            'work_area': body.work_area or instruments[body.demand_asset_id]['work_area'],
            'demand_asset_id': body.demand_asset_id, 'spare_id': body.spare_id,
            'shift_start': body.shift_start, 'shift_end': body.shift_end,
        }
        reason = dispatch.validate_transfer(candidate, instruments, certificates, travel,
                                            [serialize(item) for item in existing], report_day)
        if reason is not None:
            raise ValueError(reason)
        item = Transfer(station_id=candidate['station_id'], work_area=candidate['work_area'],
                        demand_asset_id=candidate['demand_asset_id'], spare_id=candidate['spare_id'],
                        shift_start=candidate['shift_start'], shift_end=candidate['shift_end'],
                        dataset_id=body.dataset_id, state='locked', author=actor)
        db.add(item)
        db.flush()
        record(db, actor, 'transfer.confirmed', item.id,
               station_id=item.station_id, spare_id=item.spare_id)
        # 在闸门内提交，避免其它进程在提交前读到旧状态后重复占位。
        db.commit()
        return item


def release(db, transfer_id, actor, note):
    item = db.get(Transfer, transfer_id, with_for_update=True)
    if item is None:
        raise ValueError('找不到该调拨记录')
    if item.released_at is not None:
        raise ValueError('该调拨已经释放，无需重复操作')
    item.released_at = now()
    item.released_by = actor
    item.release_note = note
    item.state, item.reason = 'released', None
    record(db, actor, 'transfer.released', item.id, spare_id=item.spare_id, note=note)
    return item


def refresh(db, dataset):
    """以刚导入的最新源数据复核全部生效调拨。

    新批次代表源数据更新：原先成立的调拨一旦不再满足名称、量程、证书、
    时间或路程要求，即标记冲突并继续占用备用具与该班次，释放后才重排。
    仅当该批次是同名最新导入时才复核，避免用旧快照覆盖现状。
    """
    if dataset is None:
        return []
    newest = db.scalar(select(Dataset).where(Dataset.name == dataset.name)
                       .order_by(Dataset.id.desc()).limit(1))
    if newest is None or newest.id != dataset.id:
        return []
    items = list(db.scalars(select(Transfer).where(Transfer.released_at.is_(None))))
    if not items:
        return []
    loaded = _parsed(dataset)
    changed = []
    for item in items:
        siblings = [serialize(other) for other in items
                    if other.id != item.id and other.spare_id == item.spare_id]
        if loaded is None:
            reason = '所属数据批次已不可用'
        else:
            instruments, certificates, _, travel_rows = loaded
            reason = dispatch.validate_transfer(
                serialize(item), instruments, certificates, dispatch.parse_travel(travel_rows),
                siblings, item.shift_start.date())
        state = 'conflict' if reason else 'locked'
        if item.state != state or item.reason != reason:
            item.state, item.reason = state, reason
            changed.append(item)
    return changed


def _parsed(dataset):
    if dataset is None:
        return None
    try:
        return parse(dataset.rows)
    except ValueError:
        return None


def locks(db):
    return [serialize(item) for item in db.scalars(
        select(Transfer).where(Transfer.released_at.is_(None)).order_by(Transfer.id))]


def in_window(db, start, end):
    return list(db.scalars(
        select(Transfer).where(Transfer.released_at.is_(None),
                               Transfer.shift_start <= end, Transfer.shift_end >= start)
        .order_by(Transfer.id)))
