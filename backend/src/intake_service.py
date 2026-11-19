from sqlalchemy import select
from .models import Intake,Consent,Agency,now
from .common import get,reject,finish,active_referral,editable

def create(db,data,user):
    reject(db.scalar(select(Intake).where(Intake.code==data.code)) is not None,'收件编号已存在')
    row=Intake(**data.model_dump());db.add(row);db.flush();db.add(Consent(intake_id=row.id,authorized=False,basis='尚未取得转介授权'));return finish(db,user,'来访建卷',row)
def revise(db,id,data,user):
    row=editable(db,id);row.facts=data.facts;row.facts_version+=1;return finish(db,user,'事实更正并更新版本',row)
def consent(db,id,data,user):
    case=get(db,Intake,id,True);row=db.scalar(select(Consent).where(Consent.intake_id==id))
    reject(case.status=='closed','销案卷宗只读')
    ref=active_referral(db,id)
    if not data.authorized and ref is not None:
        agency=get(db,Agency,ref.agency_id,True);agency.active_count-=1;ref.status='withdrawn';ref.note='来访人撤回授权，自动退回';case.status='open'
    row.authorized=data.authorized;row.basis=data.basis;row.at=now()
    spawned=[]
    if not data.authorized:
        from . import training_service as training
        from .triage_service import push
        spawned=training.revoke_sample(db,id,'样本卷授权撤回，相关旧建议作废，撤回原话不再用于训练',user)
        result=finish(db,user,'授权更新',row)
        for new in spawned:push(db,new,user,'样本撤回触发重算')
        return result
    return finish(db,user,'授权更新',row)
def authorized(db,id):
    row=db.scalar(select(Consent).where(Consent.intake_id==id))
    return row is not None and row.authorized
