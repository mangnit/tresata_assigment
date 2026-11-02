"""Run the related party completeness check on a client folder.

    python run.py                       # offline, synthetic client, writes out/
    python run.py --llm                 # uses OpenRouter (needs OPENROUTER_API_KEY)
"""
import argparse
from pathlib import Path

from rptcheck.pipeline import run

ap = argparse.ArgumentParser()
ap.add_argument("--data", default="data/kaveri")
ap.add_argument("--out", default="out")
ap.add_argument("--llm", action="store_true", help="use an OpenRouter model for Tier 3, investigator and memos")
ap.add_argument("--model", default=None)
a = ap.parse_args()

llm = None
if a.llm:
    from rptcheck.llm import OpenRouterLLM
    llm = OpenRouterLLM(model=a.model)

res = run(Path(a.data), Path(a.out), llm=llm)
print(f"{res['client']} {res['period']}: {len(res['findings'])} findings, model = {res['model']}")
for f in res["findings"]:
    inv = f"  investigator: {f['investigation']['status']}" if f["investigation"] else ""
    print(f"  [{f['status']:<9}] {f['vendor']} {f['vendor_name']:<32} {f['exception']:<48} {f['rule']}{inv}")
for c in res["calculations"]:
    for x in c["exceptions"]:
        print(f"  [calc     ] {c['party']:<36} {x['code']}: {x['detail']}")
    if c.get("exemption"):
        print(f"  [calc     ] {c['party']:<36} exempt: {c['exemption']}")
for d in res["document_flags"]:
    print(f"  [doc flag ] {d['file']}: {d['flag']}")
print(f"wrote {a.out}/findings.json, findings.csv, review.html")
