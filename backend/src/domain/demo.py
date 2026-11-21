"""澄岭精密的固定演练台账、证书历史、工位需求与调拨路程。"""

from .spec import ACTIVE, CERTIFICATE, DEMAND, DEMO_DAY, INACTIVE, INSTRUMENT, SPARE, TRAVEL

EARLY = ('08:00', '16:00')
LATE = ('20:00', '23:59')
STORAGE = '计量室'


def _row(record_type, **values):
    blank = {key: None for key in ('asset_id', 'asset_name', 'work_area', 'owner', 'asset_status',
                                   'range_min', 'range_max', 'certificate_id', 'issued_on', 'expires_on',
                                   'station_id', 'shift_start', 'shift_end', 'stop_priority',
                                   'from_area', 'to_area', 'travel_minutes')}
    return {'record_type': record_type, **blank, **values}


def _instrument(asset_id, name, area, owner, status, low, high):
    return _row(INSTRUMENT, asset_id=asset_id, asset_name=name, work_area=area, owner=owner,
                asset_status=status, range_min=low, range_max=high)


def _certificate(asset_id, code, issued, expires):
    return _row(CERTIFICATE, asset_id=asset_id, certificate_id=code, issued_on=issued, expires_on=expires)


def _demand(station_id, asset_id, area, shift, priority):
    return _row(DEMAND, station_id=station_id, asset_id=asset_id, work_area=area,
                shift_start=f'{DEMO_DAY}T{shift[0]}', shift_end=f'{DEMO_DAY}T{shift[1]}',
                stop_priority=priority)


def _travel(origin, target, minutes):
    return _row(TRAVEL, from_area=origin, to_area=target, travel_minutes=minutes)


def demo_rows() -> list[dict]:
    instruments = [
        _instrument('QC-101', '数显卡尺', '机加工区', '车削一班', ACTIVE, 0, 150),
        _instrument('QC-102', '扭矩扳手', '装配区', '装配二班', ACTIVE, 20, 200),
        _instrument('QC-103', '外径千分尺', '机加工区', '磨削班', ACTIVE, 0, 100),
        _instrument('QC-104', '针规组', '终检区', '终检班', ACTIVE, 1, 10),
        _instrument('QC-105', '电子天平', '终检区', '来料检验班', ACTIVE, 0, 2000),
        _instrument('QC-106', '高度规', '终检区', '终检班', ACTIVE, 0, 300),
        _instrument('QC-107', '指针式压力表', '装配区', '装配二班', INACTIVE, 0, 1.6),
        _instrument('SP-201', '数显卡尺', STORAGE, '计量室', SPARE, 0, 150),
        _instrument('SP-202', '数显卡尺', STORAGE, '计量室', SPARE, 0, 150),
        _instrument('SP-203', '扭矩扳手', STORAGE, '计量室', SPARE, 0, 300),
        _instrument('SP-204', '高度规', STORAGE, '计量室', SPARE, 0, 300),
        _instrument('SP-205', '外径千分尺', STORAGE, '计量室', SPARE, 0, 50),
    ]
    certificates = [
        _certificate('QC-103', 'CL-103-26', '2026-06-05', '2026-12-05'),
        _certificate('QC-101', 'CL-101-27', '2026-11-22', '2027-05-22'),
        _certificate('QC-105', 'CL-105-26', DEMO_DAY, '2027-05-21'),
        _certificate('QC-101', 'CL-101-26', '2026-05-20', '2026-11-20'),
        _certificate('QC-101', 'CL-101-25', '2025-11-20', '2026-05-20'),
        _certificate('QC-102', 'CL-102-26', '2026-05-21', DEMO_DAY),
        _certificate('QC-104', 'CL-104-26', '2026-06-06', '2026-12-06'),
        _certificate('QC-106', 'CL-106-26', '2026-11-22', '2027-05-22'),
        _certificate('QC-107', 'CL-107-25', '2025-11-05', '2026-05-05'),
        _certificate('SP-201', 'CL-S201-26', '2026-05-01', '2027-04-30'),
        _certificate('SP-202', 'CL-S202-26', '2025-11-15', '2026-11-20'),
        _certificate('SP-203', 'CL-S203-26', '2026-05-01', '2027-04-30'),
        _certificate('SP-204', 'CL-S204-26', '2026-05-01', '2027-04-30'),
        _certificate('SP-205', 'CL-S205-26', '2026-05-01', '2027-04-30'),
    ]
    demands = [
        _demand('ST-01', 'QC-101', '机加工区', EARLY, 1),
        _demand('ST-01', 'QC-101', '机加工区', LATE, 2),
        _demand('ST-02', 'QC-102', '装配区', EARLY, 1),
        _demand('ST-02', 'QC-102', '装配区', LATE, 1),
        _demand('ST-03', 'QC-106', '终检区', EARLY, 3),
        _demand('ST-03', 'QC-106', '终检区', LATE, 4),
        _demand('ST-04', 'QC-106', '终检区', EARLY, 5),
        _demand('ST-04', 'QC-106', '终检区', LATE, 2),
    ]
    travel = [
        _travel(STORAGE, '机加工区', 10),
        _travel(STORAGE, '装配区', 15),
        _travel(STORAGE, '终检区', 20),
        _travel('机加工区', '装配区', 12),
        _travel('机加工区', '终检区', 18),
        _travel('装配区', '终检区', 10),
    ]
    return instruments + certificates + demands + travel
