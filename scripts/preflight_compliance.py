"""Offline V5 risk screen. Passing is not a factual or legal certification."""
import argparse, hashlib, json, re
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from scripts.review_contract import snapshot_errors

STANDARDS={'GB/T 3863-2008':{'status':'withdrawn','replacement':'GB/T 3863-2025'},'GB/T 3863-2025':{'status':'current'},'SC/T 6055-2015':{'status':'known'}}
HOOK_TYPES={'pain_question','identity_scenario','cognitive_gap','concrete_delivery','suspense_positioning'}
FIELD_ASSERTION=re.compile(r'笔者在现场看到|实地走访中|作者来到现场|我(?:自己)?(?:这些年)?实地跑过|我在现场(?:看到|发现)|我们走访了|现场人员(?:告诉|对我说)')
PROMISE=re.compile(r'(?:免费|回复|领取|获取).{0,35}(?:测算表|测算模型|选型手册|代码包|资源包|工具包|Excel|PDF|源码|权重)',re.I)
BAIT=re.compile(r'大家怎么看[？?]?|你见过吗[？?]?|欢迎在评论区留言|评论区告诉我')

def strings(value):
    if isinstance(value,str): yield value
    elif isinstance(value,dict):
        for item in value.values(): yield from strings(item)
    elif isinstance(value,list):
        for item in value: yield from strings(item)

def topic_errors(spec):
    errors=[]
    for key in ('target_reader','reader_scenario','specific_barrier','core_value_promise'):
        if not isinstance(spec.get(key),str) or not spec[key].strip():errors.append('选题缺少 '+key)
    hooks=spec.get('hook_bank', [])
    if not isinstance(hooks,list) or any(not isinstance(h,dict) or not isinstance(h.get('title'),str) or not h['title'].strip() for h in hooks):
        errors.append('可选标题候选须为非空标题对象列表')
    elif len({h['title'].strip() for h in hooks}) != len(hooks):
        errors.append('标题候选不能重复')
    questions=spec.get('preflight_three_questions',{})
    if not isinstance(questions,dict) or any(not isinstance(questions.get(k),str) or not questions[k].strip() for k in ('who_clicks','what_gained','why_share')):
        errors.append('选题三问缺失')
    return errors

def stripping_text(content):
    lines=[]
    if content.get('lead_quote'):lines.append(content['lead_quote'])
    for sec in content.get('sections',[]):
        for key in ('paragraphs','extra_paragraphs_after_image','extra_paragraphs'):
            lines.extend(sec.get(key,[]))
        if sec.get('jackie_observation'):lines.append(sec['jackie_observation'])
    lines.extend(content.get('benchmarks',[]))
    return '\n\n'.join(re.sub('<[^>]+>','',t) for t in lines)

def resource_errors(folder, content):
    errors=[];card=content.get('industry_toolkit') or content.get('resource_card') or {}
    keyword=card.get('keyword')
    if not isinstance(keyword,str) or not keyword.strip():errors.append('资源承诺缺少单一口令 keyword')
    try:
        from scripts.release_contract import verify_manifest
        root=ROOT
        ref=card.get('release_manifest')
        if not ref:raise ValueError('未绑定资源清单')
        path=(root/ref).resolve()
        if not path.is_relative_to(root/'release_packs'):raise ValueError('资源清单位于release_packs之外')
        manifest=verify_manifest(path)
        if manifest.get('product_level')!='validated' or not manifest.get('production_ready'):
            raise ValueError('资源尚未完成功能验收，演示骨架不能兑现完整资源承诺')
        delivery=json.loads((folder/'resource_delivery.json').read_text(encoding='utf-8'))
        if (delivery.get('status')!='verified' or delivery.get('keyword')!=keyword
            or delivery.get('release_manifest_sha256')!=hashlib.sha256(path.read_bytes()).hexdigest()
            or not re.fullmatch(r'https://[^\s]+',delivery.get('resource_url',''))):raise ValueError('领取与资源版本回执不完整')
        errors.extend(snapshot_errors(folder,delivery,'资源领取证据'))
    except (OSError,ValueError,TypeError,KeyError) as exc:errors.append('资源承诺无法兑现：'+str(exc))
    return errors

def audit(folder, content=None, track='A'):
    folder=Path(folder).resolve();errors=[];warnings=[];checks=[]
    try:
        content=content or json.loads((folder/'content.json').read_text(encoding='utf-8'))
        text='\n'.join(strings(content))
        found=sorted(set(re.findall(r'(?:GB|SC)\s*/?\s*T\s*\d+\s*[-—–]\s*\d{4}',text,re.I)))
        for raw in found:
            number=re.sub(r'\s+','',raw).upper().replace('—','-').replace('–','-')
            number=re.sub(r'^(GB|SC)/?T',r'\1/T ',number)
            state=STANDARDS.get(number)
            if state and state['status']=='withdrawn':errors.append('废止标准：'+number+'，应核对 '+state['replacement'])
            elif not state:warnings.append('标准不在本地时效字典，须查询官方状态：'+number)
        checks.append({'name':'standard_status','passed':not any('废止标准' in e for e in errors)})
        if FIELD_ASSERTION.search(text):
            field=content.get('field_evidence',{})
            if not isinstance(field,dict) or field.get('status')!='verified' or not field.get('photo_paths'):
                errors.append('现场调查断言缺少field_evidence证据链，应改为核验语态')
            else:
                errors.extend(snapshot_errors(folder,field,'实地调查证据'))
                for rel in field['photo_paths']:
                    path=(folder/rel).resolve()
                    if not path.is_relative_to(folder) or not path.is_file():errors.append('现场照片不存在或越界')
        if PROMISE.search(text):errors.extend(resource_errors(folder,content))
        tail='\n'.join(strings(content.get('sections',[])[-1:]))+'\n'+'\n'.join(content.get('benchmarks',[]))
        if BAIT.search(tail):errors.append('文末低质诱导互动：请自然陈述收尾')
        if re.search(r'(?:token|tempkey|authorization)\s*[=:]\s*[A-Za-z0-9_-]{12,}',text,re.I):errors.append('正文疑似泄露凭据')
        if track=='A':
            try:errors.extend(topic_errors(json.loads((folder/'topic_spec.json').read_text(encoding='utf-8'))))
            except (OSError,ValueError):errors.append('缺少有效topic_spec.json')
            stripped=stripping_text(content)
            if not stripped.strip():errors.append('剥离图卡后无独立正文')
            review=content.get('stripping_review',{})
            if isinstance(review,dict) and review.get('status')=='rejected':errors.append('剥离审读已完成但未批准：'+str(review.get('note','')))
            elif not isinstance(review,dict) or review.get('status')!='approved':errors.append('剥离测试尚未完成纯文本审读')
            elif not all(review.get(k) for k in ('reviewer','reviewer_identity','reviewed_at')):errors.append('剥离审读缺少真实审核身份、范围或时间')
            elif review.get('text_sha256')!=hashlib.sha256(stripped.encode()).hexdigest():errors.append('剥离测试对应的正文已改变')
            else:errors.extend(snapshot_errors(folder,review,'剥离审读证据'))
            # H-P-I-P-A is a historical planning aid. It is never required for each section.
        warnings.append('规则扫描与摘要只能核验结构和证据完整性，不自动证明事实、版权、平台规则或读者价值。')
    except (OSError,ValueError,TypeError,AttributeError,KeyError) as exc:errors.append('合规输入无效：'+str(exc))
    return {'passed':not errors,'errors':list(dict.fromkeys(errors)),'warnings':warnings,'checks':checks,'scope':'offline_rule_and_evidence_screen'}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--dir',required=True);parser.add_argument('--track',default='A')
    parser.add_argument('--stripped-out');args=parser.parse_args()
    result=audit(args.dir,track=args.track)
    if args.stripped_out:
        content=json.loads((Path(args.dir)/'content.json').read_text(encoding='utf-8'))
        Path(args.stripped_out).write_text(stripping_text(content),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['passed'] else 1)
if __name__=='__main__':main()
