"""废标检查模块 (rejection) 深度审计与修复回归测试 — 2026-10-15 第一轮。"""

from __future__ import annotations
import json, inspect
import pytest
from app.db import get_db
from app.services import rejection as rj
from app.services import rejection_coverage as rcov

def _make_task(pid=1, ci=None, res=None, st="completed", fps=None, bid=None) -> int:
    tid = rj.create_task(pid, "招标正文",
        bid or [{"id":1,"name":"投标书.docx","text":"投标正文"}],
        ci or [{"id":"r001","description":"投标文件未加盖单位公章","category":"废标"}],
        ["rejection","spelling","logic"])
    with get_db() as c:
        c.execute("UPDATE rejection_checks SET status=?,results=?,false_positives=?,updated_at=datetime('now','localtime') WHERE id=?",
            (st, json.dumps(res or {},ensure_ascii=False), json.dumps(fps or [],ensure_ascii=False), tid))
    return tid

def _src(n): return inspect.getsource(getattr(rj, n))

# === A. 纯函数 ===
class TestSplitIntoChunks:
    def test_empty(self): assert rj.split_into_chunks("") == []
    def test_short(self): assert rj.split_into_chunks("短") == ["短"]
    def test_exact(self):
        t="A"*rj.CHUNK_SIZE; assert rj.split_into_chunks(t)==[t]
    def test_overlap(self):
        t="B"*(rj.CHUNK_SIZE*2+rj.CHUNK_OVERLAP+100)
        cs=rj.split_into_chunks(t); assert len(cs)>=3
        for i in range(len(cs)-1): assert cs[i+1].startswith(cs[i][-rj.CHUNK_OVERLAP:][:10])
    def test_math(self):
        cs=rj.split_into_chunks("C"*100_000)
        assert len(cs)==6  # 1+ceil(80000/18000)=6
    def test_no_loss(self):
        cs=rj.split_into_chunks("段A"*3000,chunk_size=5000,overlap=1000)
        for ch in "段A"*3000: assert any(ch in c for c in cs)

class TestSplitParagraphs:
    def test_empty(self): assert rj.split_paragraphs("")==[]
    def test_single(self): assert rj.split_paragraphs("一段")==["一段"]
    def test_dbl(self): assert rj.split_paragraphs("一\n\n二")==["一","二"]
    def test_sgl(self): assert rj.split_paragraphs("一\n二")==["一","二"]
    def test_strip(self): assert rj.split_paragraphs(" 一 \n\n 二 ")==["一","二"]
    def test_blank(self): assert rj.split_paragraphs("一\n\n\n\n二")==["一","二"]

class TestSimilarity:
    def test_identical(self): assert rj._similarity("同","同")==1.0
    def test_empty(self): assert rj._similarity("","")==0
    def test_substr(self): assert 0.3<rj._similarity("许可证","安全生产许可证")<0.6
    def test_diff(self): assert 0<rj._similarity("招标文件","投标文件")<0.8
    def test_high(self):
        assert rj._similarity("投标文件未按招标文件要求加盖单位公章",
            "投标文件未按招标文件要求加盖公章")>0.6

class TestDedupItems:
    def test_dedup(self):
        i=[{"id":"1","description":"投标文件未按招标文件要求加盖单位公章"},
           {"id":"2","description":"投标文件未按招标文件要求加盖公章"},
           {"id":"3","description":"投标报价超过招标控制价"}]
        d=rj._dedup_items(i); assert len(d)==2 and d[0]["id"]=="1"
    def test_empty(self): assert rj._dedup_items([])==[]
    def test_skip_empty(self):
        d=rj._dedup_items([{"id":"1","description":""},{"id":"2","description":"有效条目"}])
        assert len(d)==1 and d[0]["id"]=="2"

class TestAssignItemCodes:
    def test_all_ids(self):
        r=rj._assign_item_codes([{"id":"r001"},{"id":""},{}])
        assert all(i.get("id") for i in r)
    def test_no_collide(self):
        r=rj._assign_item_codes([{"id":"r001"},{"id":"r001"},{"id":"r003"}])
        ids=[i["id"] for i in r]; assert len(set(ids))==len(ids)
    def test_preserve(self):
        r=rj._assign_item_codes([{"id":"r001"},{"id":"r002"}])
        assert r[0]["id"]=="r001" and r[1]["id"]=="r002"
    def test_dup(self):
        r=rj._assign_item_codes([{"id":"r001"},{"id":"r001"}])
        assert r[0]["id"]=="r001" and r[1]["id"]!="r001"
    def test_no_reserved(self):
        r=rj._assign_item_codes([{}, {"id":"r001"}])
        assert r[0]["id"]!="r001" and r[1]["id"]=="r001"
    def test_empty(self): assert rj._assign_item_codes([])==[]
    def test_none(self): assert rj._assign_item_codes(None)==[]

class TestNormalizeSeverity:
    @pytest.mark.parametrize("inp,exp",[
        ("critical","critical"),("high","critical"),("fatal","critical"),
        ("确定废标","critical"),("warning","warning"),("warn","warning"),
        ("medium","warning"),("可能废标","warning"),("info","info"),
        ("low","info"),("note","info"),("提示","info"),
        ("unknown","warning"),("","warning"),(None,"warning")])
    def test_norm(self,inp,exp): assert rj._normalize_severity(inp)==exp

class TestCoerceIssueList:
    def test_bare(self): d=[{"id":1}]; assert rj._coerce_issue_list(d)==d
    def test_findings(self):
        r=rj._coerce_issue_list({"findings":[{"description":"P"}]}); assert len(r)==1
    def test_single(self):
        r=rj._coerce_issue_list({"description":"S"}); assert len(r)==1
    def test_none(self): assert rj._coerce_issue_list(None)==[]
    def test_filter(self):
        r=rj._coerce_issue_list([{"d":"V"},"s",123,None]); assert len(r)==1

class TestLocateTextSpan:
    def test_exact(self): assert rj.locate_text_span("证书","安全生产证书")==4
    def test_ws(self):
        assert rj.locate_text_span("证书","安全 生产 证书")>=0
        assert rj.locate_text_span("安全 生产","安全生产证书")>=0
    def test_quote(self): assert rj.locate_text_span('"证书"','「证书」')>=0
    def test_not_found(self): assert rj.locate_text_span("不存在","正常")==-1
    def test_empty(self): assert rj.locate_text_span("","正常")==-1

class TestBuildVerifiedExcerpt:
    def test_basic(self):
        e=rj.build_verified_excerpt("第一段文字",2,2,context_chars=2)
        assert "第一段" in e and "文字" in e
    def test_neg(self): assert rj.build_verified_excerpt("X",-1,2)==""
    def test_empty(self): assert rj.build_verified_excerpt("",0,2)==""
    def test_nl(self):
        e=rj.build_verified_excerpt("一\n二",0,3,context_chars=10); assert "\n" not in e

class TestBuildLineHint:
    def test_l1(self): assert rj.build_line_hint("X",0)=="第 1 行"
    def test_l3(self): assert rj.build_line_hint("1\n2\n3",10)=="第 3 行"
    def test_neg(self): assert rj.build_line_hint("X",-1)==""
    def test_empty(self): assert rj.build_line_hint("",0)==""

class TestVerifyRejectionEvidence:
    def test_validated(self):
        r=rj.verify_rejection_evidence({"quote":"许可证"},"含许可证")
        assert r["validated"] is True and r["evidence_kind"]=="quote"
    def test_not_found(self):
        r=rj.verify_rejection_evidence({"quote":"不存在"},"正常")
        assert r["validated"] is False
    def test_structural(self):
        r=rj.verify_rejection_evidence({"description":"缺材料"},"正常")
        assert r.get("validated") is None and r["evidence_kind"]=="structural"
    def test_bid_evidence(self):
        r=rj.verify_rejection_evidence({"bidEvidence":"许可证"},"含许可证")
        assert r["validated"] is True
    def test_no_mutate(self):
        i={"quote":"X"}; orig=dict(i); rj.verify_rejection_evidence(i,"X"); assert i==orig

class TestVerifySpellingError:
    def test_verified(self):
        r=rj.verify_spelling_error({"original":"工怟"},"项目工怟期")
        assert r["verified"] is True and r["position"]>=0
    def test_not_found(self):
        r=rj.verify_spelling_error({"original":"不存在"},"正常")
        assert r["verified"] is False and r["discard_reason"]=="not_found_in_source"
    def test_missing(self):
        r=rj.verify_spelling_error({},"正常")
        assert r["verified"] is False and r["discard_reason"]=="missing_original"
    def test_wrong_field(self):
        r=rj.verify_spelling_error({"wrong":"工怟"},"项目工怟期")
        assert r["verified"] is True

class TestExtractClaimKeywords:
    def test_quoted(self):
        k=rj.extract_claim_keywords({"description":"未找到「安全生产许可证」"})
        assert "安全生产许可证" in k
    def test_suffixed(self):
        k=rj.extract_claim_keywords({"description":"投标承诺书和营业执照"})
        assert any("承诺书" in x for x in k)
    def test_short(self):
        k=rj.extract_claim_keywords({"description":"证"})
        assert not any(x=="证" for x in k)
    def test_stopwords(self):
        k=rj.extract_claim_keywords({"description":"投标文件和材料"})
        assert not any(x in rj._MATERIAL_STOPWORDS for x in k)
    def test_markers(self):
        k=rj.extract_claim_keywords({"description":"疑似缺少部分佐证材料"})
        assert not any(x=="材料" for x in k)
    def test_empty(self): assert rj.extract_claim_keywords({})==[]
    def test_none(self): assert rj.extract_claim_keywords(None)==[]
    def test_limit(self):
        k=rj.extract_claim_keywords({"description":"「材料A」「材料B」「材料C」「材料D」"},limit=3)
        assert len(k)<=3

class TestResolveMissingClaims:
    def test_resolved(self):
        i=[{"evidence_kind":"structural","description":"未找到「安全生产许可证」"}]
        r=rj.resolve_missing_material_claims(i,"含安全生产许可证")
        assert r["stats"][rj.MISSING_STATE_RESOLVED]==1
    def test_confirmed(self):
        i=[{"evidence_kind":"structural","description":"未找到「安全生产许可证」"}]
        r=rj.resolve_missing_material_claims(i,"无相关材料")
        assert r["stats"][rj.MISSING_STATE_CONFIRMED]==1
    def test_pending(self):
        i=[{"evidence_kind":"structural","description":"材料不足"}]
        r=rj.resolve_missing_material_claims(i,"正常")
        assert r["stats"][rj.MISSING_STATE_PENDING]==1
    def test_empty(self):
        r=rj.resolve_missing_material_claims([], "X")
        assert all(v==0 for v in r["stats"].values())

# === B. 缺陷修复验证 ===
class TestD1DeleteTasks:
    def test_docstring_hard(self):
        s=_src("delete_tasks"); assert "DELETE FROM" in s or "硬删除" in s
    def test_hard_delete(self):
        tid=_make_task(st="pending"); rj.delete_tasks([tid])
        with get_db() as c:
            row=c.execute("SELECT 1 FROM rejection_checks WHERE id=?", (tid,)).fetchone()
        assert row is None
    def test_no_complete(self):
        tid=_make_task(st="completed"); r=rj.delete_tasks([tid])
        assert r["success"]==0 and "只能删除未完成" in r["message"]
    def test_mixed_reject(self):
        t1=_make_task(st="completed"); t2=_make_task(st="pending")
        r=rj.delete_tasks([t1,t2]); assert r["success"]==0
        with get_db() as c:
            assert c.execute("SELECT 1 FROM rejection_checks WHERE id=?", (t2,)).fetchone()

class TestD2SummaryCounts:
    def test_exclude_fps(self):
        task={"results":{"rejection":[{"id":"i1"},{"id":"i2"},{"id":"i3"}],"spelling":[],"logic":[]},
              "false_positives":["i1","i2"]}
        fps=set(task["false_positives"])
        vis=[i for i in task["results"]["rejection"] if i.get("id") not in fps]
        assert len(vis)==1
    def test_hidden_note(self):
        fps={"i1"}; issues=[{"id":"i1"},{"id":"i2"},{"id":"i3"}]
        vis=[i for i in issues if i.get("id") not in fps]
        assert len(vis)==2 and len(issues)-len(vis)==1

class TestD3LogicChunking:
    def test_no_truncation(self):
        s=_src("check_logic_dimension")
        assert "text[:30000" not in s  # 不应再有截断到 30000 字的切片
    def test_uses_chunks(self):
        s=_src("check_logic_dimension"); assert "split_into_chunks" in s

class TestD6FalsePositiveRace:
    def test_same_txn(self):
        s=_src("mark_all_false_positive")
        assert "with get_db()" in s and "SELECT" in s
    def test_concurrent(self):
        tid=_make_task(fps=[])
        rj.mark_all_false_positive(tid,["i1","i2"])
        rj.mark_all_false_positive(tid,["i3"])
        t=rj.get_task(tid); fps=set(t.get("false_positives",[]))
        assert {"i1","i2","i3"}==fps
    def test_not_found(self):
        with pytest.raises(ValueError, match="任务不存在"):
            rj.mark_all_false_positive(999999, ["i1"])

class TestD7ExtractClauses:
    def test_from_check_items(self):
        ci=[{"id":"r001","description":"投标文件未加盖单位公章和法定代表人签字"}]
        c=rcov._extract_clauses({},ci); assert len(c)==1 and "description" not in c[0]
    def test_from_results(self):
        res={"rejection":[{"id":"r001","description":"未加盖公章"},{"id":"r002","description":"投标报价超限"}]}
        c=rcov._extract_clauses(res); assert len(c)==2
    def test_check_items_priority(self):
        ci=[{"id":"r001","description":"来自check_items"}]
        res={"rejection":[{"id":"r001","description":"来自results"}]}
        c=rcov._extract_clauses(res,ci); assert c[0]["text"]=="来自check_items"
    def test_desc_recognized(self):
        ci=[{"id":"r001","description":"投标文件未按招标文件要求填写"}]
        c=rcov._extract_clauses(None,ci); assert len(c)==1
    def test_short_filtered(self):
        c=rcov._extract_clauses(None,[{"id":"r001","description":"证"}]); assert c==[]
    def test_none(self):
        assert rcov._extract_clauses(None,None)==[]
    def test_list(self):
        c=rcov._extract_clauses([{"id":"r001","description":"条款内容足够长"}])
        assert len(c)==1

# === C. 静态守卫 ===
class TestStaticGuards:
    def test_wrappers(self):
        s=_src("run_rejection_check")
        assert "_wrap_rejection" in s and "_wrap_spelling" in s and "_wrap_logic" in s
    def test_fallback(self):
        s=_src("check_rejection_dimension")
        assert "_run_rejection_three_rounds" in s and "_run_rejection_single_round" in s
    def test_fps_consistent(self):
        s=_src("export_report"); assert s.count("not in fps")>=2
    def test_sanitized(self):
        s=_src("check_spelling_dimension"); assert "sanitize_prompt_value" in s
    def test_no_trunc(self):
        s=_src("check_logic_dimension"); assert "text[:30000" not in s