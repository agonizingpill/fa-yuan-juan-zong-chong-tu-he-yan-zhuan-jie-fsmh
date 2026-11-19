import os,redis
from fastapi import HTTPException
from sqlalchemy import select
from .models import Triage,Intake,Audit
from .common import get,reject,editable,dump
from .intake_service import authorized
from .constants import TriageState
from . import training_service as training
queue=redis.Redis.from_url(os.getenv('REDIS_URL','redis://redis:6379/0'),decode_responses=True)
def latest(db,id):
    case=get(db,Intake,id)
    return db.scalar(select(Triage).where(Triage.intake_id==id,Triage.facts_version==case.facts_version,Triage.status==TriageState.reviewed.value).order_by(Triage.id.desc()))
def push(db,row,user,action):
    db.add(Audit(actor=getattr(user,'username','system'),action=action,detail=str(row.id)));db.commit()
    try:queue.rpush('aid:triage',row.id)
    except redis.RedisError:
        row.status='failed';row.error='任务队列暂不可用，恢复后可重新提交';db.commit()
        raise HTTPException(503,row.error)
    return dump(row)
def make_queue_row(db,case):
    samples=training.eligible_samples(db,case.id)
    reject(not training.enough_scope(samples),'合资格训练样本不足（至少两个主题各一条），先由主管登记确认卷宗样本')
    row=Triage(intake_id=case.id,facts_version=case.facts_version,status=TriageState.queued.value)
    db.add(row);db.flush();training.snapshot(db,row,samples)
    return row
def enqueue(db,data,user):
    case=editable(db,data.intake_id);reject(not authorized(db,case.id),'未取得授权，不能提交行政分流')
    active=db.scalar(select(Triage).where(Triage.intake_id==case.id,Triage.facts_version==case.facts_version,Triage.status.in_([TriageState.queued.value,TriageState.running.value])))
    reject(active is not None,'当前版本仍在分流')
    row=make_queue_row(db,case)
    return push(db,row,user,'提交主题分流')
def review(db,id,data,user):
    initial=get(db,Triage,id);case=get(db,Intake,initial.intake_id,True);row=get(db,Triage,id,True)
    reject(row.status==TriageState.invalid.value,'该建议已随样本撤回作废，不能复核，请使用重算结果')
    reject(row.status!=TriageState.done.value,'等待分流完成再复核')
    reject(row.facts_version!=case.facts_version,'事实已更正，请重新分流')
    if not authorized(db,case.id):
        row.status=TriageState.invalid.value;row.invalid_reason='来访授权已撤回，本建议失效'
        db.add(Audit(actor=user.username,action='复核拦截：授权撤回失效',detail=str(row.id)));db.commit()
        reject(True,'来访授权已撤回，本建议已失效')
    ok,reason=training.recheck_snapshot(db,row)
    if not ok:
        row.status=TriageState.invalid.value;row.invalid_reason=reason
        db.add(Audit(actor=user.username,action='复核拦截：样本撤回失效',detail=str(row.id)));db.commit()
        reject(True,'训练样本已变更（'+reason+'），本建议已作废，请使用重算结果')
    row.approved=data.topic;row.review_note=data.note;row.status=TriageState.reviewed.value
    db.add(Audit(actor=user.username,action='专长人工复核',detail=str(row.id)));db.commit()
    return dump(row)
