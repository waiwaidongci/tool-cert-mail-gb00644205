import logging
from datetime import date, timedelta
from sqlalchemy import and_, or_, select
from src.config.database import Session
from src.constants.enums import Status
from src.domain.analysis import analyze
from src.domain.spec import SPEC
from src.models.jobs import Delivery, Run, Schedule
from src.models.source import Dataset
from src.services import transfers
from src.services.audit import record
from src.utils.exports import write_report
from src.utils.time import now

log = logging.getLogger(__name__)


def materialize_due():
    if not SPEC.get('scheduled'):
        return
    stamp = now()
    with Session.begin() as db:
        schedules = db.scalars(select(Schedule).where(Schedule.enabled.is_(True), Schedule.next_at <= stamp)
                                .order_by(Schedule.next_at).limit(20).with_for_update(skip_locked=True))
        for item in schedules:
            due = item.next_at
            dataset_id = item.dataset_id
            params = dict(item.params)
            if item.repeat_daily:
                elapsed_days = (stamp - due).days
                due += timedelta(days=elapsed_days)
                period_key = SPEC['schedule_date_param']
                report_day = date.fromisoformat(params[period_key]) + timedelta(days=elapsed_days)
                params[period_key] = report_day.isoformat()
                item.params = {**params, period_key: (report_day + timedelta(days=1)).isoformat()}
                dataset_id = db.scalar(select(Dataset.id).order_by(Dataset.id.desc()).limit(1)) or dataset_id
                item.next_at = due + timedelta(days=1)
            else:
                item.enabled = False
            db.add(Run(dataset_id=dataset_id, schedule_id=item.id, scheduled_at=due,
                       name=item.name, params=params, author=item.author))


def claim():
    stamp = now()
    with Session.begin() as db:
        query = select(Run).where(or_(Run.status == Status.QUEUED,
                                      and_(Run.status == Status.RUNNING, Run.lease_until < stamp)))
        job = db.scalar(query.order_by(Run.id).limit(1).with_for_update(skip_locked=True))
        if job is None:
            return None
        if job.attempt >= 3:
            job.status = Status.FAILED
            job.error = '后台进程多次中断，请发起新的运行'
            job.finished_at = stamp
            record(db, 'worker', 'analysis.failed', job.id, reason=job.error)
            return None
        job.status = Status.RUNNING
        job.attempt += 1
        job.lease_until = stamp + timedelta(minutes=3)
        records = db.get(Dataset, job.dataset_id).rows
        # 在闸门内取锁定快照，与确认互斥，保证规划读到的占用集是完整的。
        with transfers.guard(db) as acquired:
            if not acquired:
                job.attempt -= 1
                job.status = Status.QUEUED
                job.lease_until = None
                return None
            locks = transfers.locks(db)
        return job.id, job.attempt, records, job.params, locks


def execute(lease):
    job_id, attempt, rows, params, locks = lease
    result = export = failure = None
    try:
        result = analyze(rows, params, locks)
        export = write_report(job_id, attempt, result)
    except ValueError as error:
        failure = str(error)[:500]
    except Exception:
        log.exception('Analysis %s failed', job_id)
        failure = '计算失败，请检查字段口径后重试'
    with Session.begin() as db:
        job = db.get(Run, job_id, with_for_update=True)
        if job.attempt != attempt or job.status != Status.RUNNING:
            return
        if not failure:
            # 在闸门内取最新锁定复核：确认与重算互斥，writes 不会交错。
            with transfers.guard(db) as acquired:
                if acquired:
                    dataset = db.get(Dataset, job.dataset_id)
                    changed = transfers.refresh(db, dataset)
                    for entry in changed:
                        record(db, 'worker', f'transfer.{entry.state}', entry.id,
                               reason=entry.reason, run_id=job.id)
                    latest = transfers.locks(db)
                    if latest != locks:
                        # claim 之后到达的确认改变了占用集，用最新锁定重算并覆盖导出。
                        try:
                            result = analyze(rows, params, latest)
                            export = write_report(job_id, attempt, result)
                        except ValueError as error:
                            failure = str(error)[:500]
                        except Exception:
                            log.exception('Analysis %s replan failed', job_id)
                            failure = '计算失败，请检查字段口径后重试'
        job.lease_until = None
        job.finished_at = now()
        job.status = Status.FAILED if failure else Status.COMPLETE
        job.result = None if failure else result
        job.export_file = None if failure else export
        job.error = failure
        record(db, 'worker', f'analysis.{job.status}', job.id, attempt=attempt, reason=failure)
        if job.schedule_id:
            plan = db.get(Schedule, job.schedule_id)
            # 分发门槛仍以证书覆盖率为准；调拨缺口在班报内逐项说明，不拦截投递。
            ready = not failure and result.get('ready', True)
            detail = '已保存 CSV 到站内收件箱' if ready else (failure or result['headline'])
            receipt = Delivery(run_id=job.id, recipient=plan.recipient,
                               state='delivered' if ready else 'blocked', detail=detail[:500])
            db.add(receipt)
            record(db, 'worker', f'delivery.{receipt.state}', job.id, recipient=plan.recipient)
