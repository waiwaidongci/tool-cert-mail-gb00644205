from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from src.config.database import connection
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.models.identity import User
from src.models.jobs import Transfer
from src.schemas.requests import ReleaseRequest, TransferRequest
from src.services import transfers
from src.utils.time import iso

router = APIRouter(prefix='/transfers')


def _view(item):
    return {**transfers.serialize(item), 'confirmed_at': iso(item.confirmed_at),
            'released_at': iso(item.released_at)}


@router.get('')
def index(db: Session = Depends(connection), actor: User = Depends(current)):
    return [_view(item) for item in db.scalars(select(Transfer).order_by(Transfer.id.desc()).limit(200))]


@router.post('', status_code=201)
def confirm(body: TransferRequest, db: Session = Depends(connection),
            actor: User = Depends(allow(Role.ADMIN))):
    try:
        item = transfers.confirm(db, body, actor.username)
    except ValueError as error:
        db.rollback()
        raise HTTPException(409, str(error)) from None
    return _view(item)


@router.post('/{transfer_id}/release')
def release(transfer_id: int, body: ReleaseRequest, db: Session = Depends(connection),
            actor: User = Depends(allow(Role.ADMIN))):
    try:
        item = transfers.release(db, transfer_id, actor.username, body.note)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    db.commit()
    return _view(item)
