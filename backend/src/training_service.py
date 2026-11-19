from sqlalchemy import select
from .models import Intake,CaseParty,TrainingSample,TriageSample,Triage,Audit,now
from .common import get,reject
from .constants import TriageState,SampleState
from .intake_service import authorized

def client_party_id(db,intake_id):
    row=db.scalar(select(CaseParty).where(CaseParty.intake_id==intake_id,CaseParty.side=='client'))
    return row.party_id if row else None

def active_sample(db,intake_id):
    return db.scalar(select(TrainingSample).where(TrainingSample.intake_id==intake_id,TrainingSample.status==SampleState.active.value))

def latest_reviewed(db,intake_id):
    case=get(db,Intake,intake_id)
    return db.scalar(select(Triage).where(Triage.intake_id==intake_id,Triage.facts_version==case.facts_version,Triage.status==TriageState.reviewed.value).order_by(Triage.id.desc()))

def register_sample(db,data,user):
    case=get(db,Intake,data.intake_id,True)
    reject(case.status=='closed','销案卷宗只读，不能登记训练样本')
    reject(not authorized(db,case.id),'未取得有效授权，不能登记训练样本')
    triage=latest_reviewed(db,case.id)
    reject(not triage,'该卷宗当前事实版本尚无主管确认的主题建议，不能登记样本')
    reject(triage.approved!=data.topic,'样本主题必须与主管确认的专长方向一致')
    row=db.scalar(select(TrainingSample).where(TrainingSample.intake_id==case.id))
    if row:
        reject(row.status==SampleState.active.value,'该卷宗已在训练样本册，撤回后才能重新登记')
        row.status=SampleState.active.value;row.topic=data.topic;row.basis=data.basis;row.revoked_at=None
    else:
        row=TrainingSample(intake_id=case.id,topic=data.topic,basis=data.basis);db.add(row)
    db.flush();db.add(Audit(actor=user.username,action='登记训练样本',detail=str(case.id)));db.commit()
    return {c.name:getattr(row,c.name) for c in row.__table__.columns}

def eligible_samples(db,target_intake_id):
    # 合资格口径：样本有效 + 样本卷授权仍在 + 与目标卷非同归一化来访方
    mine=client_party_id(db,target_intake_id)
    result=[]
    for sample in db.scalars(select(TrainingSample).where(TrainingSample.status==SampleState.active.value)):
        if not authorized(db,sample.intake_id):continue
        if mine is not None and client_party_id(db,sample.intake_id)==mine:continue
        result.append(sample)
    return result

def enough_scope(samples):
    return len(samples)>=2 and len({s.topic for s in samples})>=2

def snapshot(db,triage_row,samples):
    for sample in samples:
        db.add(TriageSample(triage_id=triage_row.id,sample_id=sample.id,intake_id=sample.intake_id,
                            topic=sample.topic,status_at_snapshot=SampleState.active.value))

def recheck_snapshot(db,row):
    # 排队后样本可能被撤回或当事人被改派；逐条标记排除原因，返回是否仍全部有效。
    # 重算任务在创建时已主动标记的排除项不再重复判失效。
    fresh=False
    for link in db.scalars(select(TriageSample).where(TriageSample.triage_id==row.id)):
        if link.excluded:continue
        why=''
        sample=db.get(TrainingSample,link.sample_id)
        if sample is None or sample.status!=SampleState.active.value:why='样本卷撤回授权，已移出训练集'
        elif not authorized(db,link.intake_id):why='样本卷授权已失效'
        else:
            mine=client_party_id(db,row.intake_id)
            if mine is not None and client_party_id(db,link.intake_id)==mine:why='同来访方旧卷隔离，不能用于本卷'
        if why:
            link.excluded=True;link.exclude_reason=why;fresh=True
    return not fresh,'' if not fresh else next((l.exclude_reason for l in db.scalars(select(TriageSample).where(TriageSample.triage_id==row.id,TriageSample.excluded==True))),'样本已变更')

def bundle_from_snapshot(db,row):
    links=db.scalars(select(TriageSample).where(TriageSample.triage_id==row.id,TriageSample.excluded==False)).all()
    return [(db.get(Intake,link.intake_id).facts,link.topic) for link in links]

def spawn_recompute(db,old,reason):
    # 仅用原批其余合资格样本重建排队任务；不足则不自动重算，但在旧建议上注明
    eligible=eligible_samples(db,old.intake_id)
    old.status=TriageState.invalid.value
    if not enough_scope(eligible):
        old.invalid_reason=reason+'；其余合资格样本不足两个主题，未自动重算，请人工处理'
        return None
    old.invalid_reason=reason
    row=Triage(intake_id=old.intake_id,facts_version=old.facts_version,status=TriageState.queued.value,recomputed_from=old.id)
    db.add(row);db.flush()
    eligible_ids={s.id for s in eligible}
    # 留痕保留原批全部样本：仍合资格的采用，撤回/隔离的标排除原因
    for link in db.scalars(select(TriageSample).where(TriageSample.triage_id==old.id).order_by(TriageSample.id)):
        sample=db.get(TrainingSample,link.sample_id)
        if link.sample_id in eligible_ids:
            db.add(TriageSample(triage_id=row.id,sample_id=link.sample_id,intake_id=link.intake_id,
                                topic=link.topic,status_at_snapshot=SampleState.active.value))
        else:
            why=reason
            if sample is None or sample.status!=SampleState.active.value:why='样本卷撤回授权，已移出训练集'
            db.add(TriageSample(triage_id=row.id,sample_id=link.sample_id,intake_id=link.intake_id,
                                topic=link.topic,status_at_snapshot=SampleState.active.value,excluded=True,exclude_reason=why))
    db.add(Audit(actor='system',action='样本撤回触发重算',detail=f'{old.id}->{row.id}'))
    return row

def invalidate_pending(db,sample,reason):
    # 排队/计算中/待复核的旧建议一律作废；已确认的触发重算，返回新任务供提交后入队
    linked_ids=select(TriageSample.triage_id).where(TriageSample.sample_id==sample.id)
    rows=db.scalars(select(Triage).where(Triage.id.in_(linked_ids))).all()
    spawned=[]
    for row in rows:
        link=db.scalar(select(TriageSample).where(TriageSample.triage_id==row.id,TriageSample.sample_id==sample.id))
        if link and not link.excluded:
            link.excluded=True;link.exclude_reason=reason
        if row.status==TriageState.reviewed.value:
            new=spawn_recompute(db,row,reason)
            if new:spawned.append(new)
        elif row.status in (TriageState.queued.value,TriageState.running.value,TriageState.done.value):
            row.status=TriageState.invalid.value;row.invalid_reason=reason
            db.add(Audit(actor='system',action='排队分流随样本撤回作废',detail=str(row.id)))
    return spawned

def revoke_sample(db,intake_id,reason,user):
    sample=active_sample(db,intake_id)
    if not sample:return []
    sample.status=SampleState.revoked.value;sample.revoked_at=now()
    spawned=invalidate_pending(db,sample,reason)
    db.add(Audit(actor=user.username,action='撤回训练样本授权',detail=f'{intake_id}: {reason}'))
    db.flush()
    return spawned

def trace(db,triage_id):
    row=get(db,Triage,triage_id)
    links=db.scalars(select(TriageSample).where(TriageSample.triage_id==row.id).order_by(TriageSample.id)).all()
    samples=[]
    for link in links:
        case=db.get(Intake,link.intake_id)
        sample=db.get(TrainingSample,link.sample_id)
        samples.append({'sample_id':link.sample_id,'intake_id':link.intake_id,'intake_code':case.code,'alias':case.alias,
                        'topic':link.topic,'status_at_snapshot':link.status_at_snapshot,
                        'current_status':sample.status if sample else 'missing','excluded':link.excluded,'exclude_reason':link.exclude_reason})
    parent=None
    if row.recomputed_from:
        old=db.get(Triage,row.recomputed_from)
        parent={'triage_id':old.id,'status':old.status,'invalid_reason':old.invalid_reason}
    children=[{'triage_id':t.id,'status':t.status,'invalid_reason':t.invalid_reason}
              for t in db.scalars(select(Triage).where(Triage.recomputed_from==row.id).order_by(Triage.id))]
    return {'triage':{c.name:getattr(row,c.name) for c in row.__table__.columns},
            'samples':samples,'recomputed_from':parent,'recomputed_to':children}
