# SPDX-License-Identifier: Apache-2.0
from dotenv import load_dotenv; load_dotenv('.env', override=True)
import time, json, re, os, zipfile
from connectors.mcp_bridge import _build_document, _analyze_data, _revise_artifact, _list_document_versions
from workers.doc_worker import _R, DOC_DIR
_STATUS_OK   = "OK"
_STATUS_FAIL = "FAIL"
def wait(jid,t=90):
    for _ in range(t//2):
        raw=_R.get(f"doc:result:{jid}")
        if raw: return json.loads(raw)
        time.sleep(2)
    return {"status":"timeout"}
def jid_of(t): m=re.search(r"\[DOCJOB:([0-9a-f-]+):",t); return m.group(1) if m else None
def aid_of(t): m=re.search(r"artifact_id is `([0-9a-f-]+)`",t); return m.group(1) if m else None
R={}
# multimodal
t=time.time()
CODE='''const fs=require('fs');const {Document,Packer,Paragraph,TextRun,ImageRun,HeadingLevel}=require('docx');
const img=fs.readFileSync('cover.png');
Packer.toBuffer(new Document({sections:[{children:[new Paragraph({heading:HeadingLevel.HEADING_1,children:[new TextRun('UPI 2026')]}),new Paragraph({children:[new ImageRun({data:img,transformation:{width:440,height:248}})]})]}]})).then(b=>fs.writeFileSync('output.docx',b));'''
res=wait(jid_of(_build_document("demo",{"format":"docx","title":"MM","code":CODE,"images":[{"name":"cover.png","prompt":"abstract fintech network navy teal no text","aspect_ratio":"16:9"}]})["content"][0]["text"]))
R["#13 Multimodal"]=(_STATUS_OK if res.get("status")=="done" and (res.get("size") or 0)>80000 else _STATUS_FAIL, f"{time.time()-t:.0f}s {res.get('size')}b")
# ADA
t=time.time()
r=_analyze_data({"data":"m,v\nApr,13.1\nMay,13.55\nJun,14.02\n","filename":"d.csv","code":"import csv;rows=list(csv.DictReader(open('d.csv')));print('peak',max(rows,key=lambda x:float(x['v']))['m'])"})
R["#7 ADA"]=(_STATUS_OK if not r.get("isError") and "peak Jun" in r["content"][0]["text"] else _STATUS_FAIL, f"{time.time()-t:.0f}s {r['content'][0]['text'][:40].strip()}")
# versioning+canvas
t=time.time()
DOCX='''const fs=require('fs');const {Document,Packer,Paragraph,TextRun}=require('docx');Packer.toBuffer(new Document({sections:[{children:[new Paragraph({children:[new TextRun('ORIGINAL')]})]}]})).then(b=>fs.writeFileSync('output.docx',b));'''
b=_build_document("demo",{"format":"docx","title":"Canvas","code":DOCX}); aid=aid_of(b["content"][0]["text"]); wait(jid_of(b["content"][0]["text"]))
rv=_revise_artifact("demo",{"artifact_id":aid,"instruction":"Change the text to exactly 'REVISED BY AI'."}); res2=wait(jid_of(rv["content"][0]["text"]))
lst=_list_document_versions("demo",{"artifact_id":aid})["content"][0]["text"]
ok=False
try:
    f=os.path.join(DOC_DIR,res2.get("file_id","")+".docx")
    if os.path.exists(f): ok="REVISED BY AI" in re.sub('<[^>]+>','',zipfile.ZipFile(f).read('word/document.xml').decode('utf-8','replace'))
except: pass
R["#9/#11 Version+Canvas"]=(_STATUS_OK if "2 version" in lst and res2.get("version")==2 and ok else _STATUS_FAIL, f"{time.time()-t:.0f}s edit_applied={ok}")
# xlsx
t=time.time()
res3=wait(jid_of(_build_document("demo",{"format":"xlsx","title":"S","code":"from openpyxl import Workbook\nwb=Workbook();wb.active['A1']='UPI';wb.save('output.xlsx')"})["content"][0]["text"]))
R["Core xlsx"]=(_STATUS_OK if res3.get("status")=="done" else _STATUS_FAIL, f"{time.time()-t:.0f}s {res3.get('size')}b")
print("\n"+"="*55+"\nDOC-PIPELINE RE-TEST\n"+"="*55)
for k,(v,d) in R.items(): print(f"  [{v}] {k:24s} — {d}")
print("ALL PASS" if all(v == _STATUS_OK for v, _ in R.values()) else "STILL FAILING")
