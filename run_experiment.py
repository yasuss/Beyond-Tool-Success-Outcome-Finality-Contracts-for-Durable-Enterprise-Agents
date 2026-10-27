#!/usr/bin/env python3
import csv, json, hashlib
from itertools import product
from pathlib import Path
ROOT=Path(__file__).resolve().parent

def stable_hash(obj):
    return hashlib.sha256(json.dumps(obj,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def call_external(state, supports_idempotency, use_key):
    if supports_idempotency and use_key:
        if state['idempotency_seen']:
            return
        state['idempotency_seen']=True
    state['effect_count'] += 1

def blind_retry(case):
    state={'effect_count': 1 if case['initial_applied'] else 0, 'idempotency_seen':False, 'status':'UNKNOWN'}
    # Blind retry does not establish or use a semantic idempotency contract.
    call_external(state, case['idempotency_support'], use_key=False)
    if case['duplicate_dispatch']:
        call_external(state, case['idempotency_support'], use_key=False)
    state['status']='ASSUMED_APPLIED'
    return state

def idempotency_only(case):
    state={'effect_count': 1 if case['initial_applied'] else 0, 'idempotency_seen':bool(case['initial_applied'] and case['idempotency_support']), 'status':'UNKNOWN'}
    call_external(state, case['idempotency_support'], use_key=True)
    if case['duplicate_dispatch']:
        call_external(state, case['idempotency_support'], use_key=True)
    state['status']='ASSUMED_APPLIED'
    return state

def reconcile_first(case):
    state={'effect_count': 1 if case['initial_applied'] else 0, 'idempotency_seen':bool(case['initial_applied'] and case['idempotency_support']), 'status':'UNKNOWN'}
    def handler():
        if state['status'] in ('CONFIRMED_APPLIED','MANUAL_REVIEW'):
            return
        if case['readback_support']:
            if state['effect_count'] >= 1:
                state['status']='CONFIRMED_APPLIED'
                return
            call_external(state, case['idempotency_support'], use_key=True)
            state['status']='CONFIRMED_APPLIED'
            return
        if case['idempotency_support']:
            call_external(state, True, use_key=True)
            state['status']='CONFIRMED_APPLIED'
            return
        state['status']='MANUAL_REVIEW'
    handler()
    if case['duplicate_dispatch']:
        handler()
    return state

strategies={'BLIND_RETRY':blind_retry,'IDEMPOTENCY_ONLY':idempotency_only,'RECONCILE_FIRST':reconcile_first}
rows=[]
for initial_applied,idemp,readback,dup in product([False,True], repeat=4):
    case={'initial_applied':initial_applied,'idempotency_support':idemp,'readback_support':readback,'duplicate_dispatch':dup}
    case_id=f"A{int(initial_applied)}-I{int(idemp)}-R{int(readback)}-D{int(dup)}"
    for name,fn in strategies.items():
        s=fn(case)
        rows.append({**case,'case_id':case_id,'strategy':name,'effect_count':s['effect_count'],'duplicate_effect':s['effect_count']>1,'manual_review':s['status']=='MANUAL_REVIEW','resolved':s['status']=='CONFIRMED_APPLIED','status':s['status']})

with open(ROOT/'effect_retry_results.csv','w',newline='') as f:
    w=csv.DictWriter(f,fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)

summary={}
for name in strategies:
    rr=[r for r in rows if r['strategy']==name]
    summary[name]={
      'cases':len(rr),
      'duplicate_effect_cases':sum(r['duplicate_effect'] for r in rr),
      'manual_review_cases':sum(r['manual_review'] for r in rr),
      'resolved_cases':sum(r['resolved'] for r in rr),
      'max_effect_count':max(r['effect_count'] for r in rr),
    }

# Business completion semantics: ground truth is the workflow-specific business postcondition.
completion=[]
for effect_confirmed,correction_observed,erp_reconciled in product([False,True], repeat=3):
    truth=correction_observed and erp_reconciled
    preds={
      'EFFECT_CONFIRMED_COMPLETION': effect_confirmed,
      'CORRECTION_ONLY_COMPLETION': correction_observed,
      'BUSINESS_POSTCONDITION_COMPLETION': correction_observed and erp_reconciled,
    }
    cid=f"E{int(effect_confirmed)}-C{int(correction_observed)}-R{int(erp_reconciled)}"
    for name,pred in preds.items():
        completion.append({'case_id':cid,'effect_confirmed':effect_confirmed,'correction_observed':correction_observed,'erp_reconciled':erp_reconciled,'strategy':name,'ground_truth_complete':truth,'predicted_complete':pred,'false_positive':pred and not truth,'false_negative':truth and not pred})
with open(ROOT/'completion_results.csv','w',newline='') as f:
    w=csv.DictWriter(f,fieldnames=list(completion[0])); w.writeheader(); w.writerows(completion)
completion_summary={}
for name in sorted(set(r['strategy'] for r in completion)):
    rr=[r for r in completion if r['strategy']==name]
    completion_summary[name]={'cases':len(rr),'false_positive_cases':sum(r['false_positive'] for r in rr),'false_negative_cases':sum(r['false_negative'] for r in rr),'correct_cases':sum(not r['false_positive'] and not r['false_negative'] for r in rr)}

out={'schema':'EFFECT_OUTCOME_EXPERIMENT_v1','deterministic':True,'effect_retry_summary':summary,'completion_summary':completion_summary,'design':{'effect_retry_dimensions':['initial_applied','idempotency_support','readback_support','duplicate_dispatch'],'completion_dimensions':['effect_confirmed','correction_observed','erp_reconciled'],'effect_retry_cases':16,'completion_cases':8},'limitations':['Executable semantic model only; no SAP, portal, email-provider, or production connector behavior is measured.','Read-after-write is modeled as authoritative when available; stale/eventually-consistent reads require connector-specific handling beyond this study.','No latency, cost, or human-factors claims are made.']}
(ROOT/'summary.json').write_text(json.dumps(out,indent=2)+"\n")
print(json.dumps(out,indent=2))
