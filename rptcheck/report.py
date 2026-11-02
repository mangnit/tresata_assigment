"""Static review page (S8 mock): one card per finding, with the full evidence trail.
Accept / reject buttons are a mock; in production decisions would be written back
as human confirmed edges with reviewer name and reason."""
from __future__ import annotations

import html
from pathlib import Path

CSS = """
body{font:14px/1.5 system-ui,sans-serif;margin:0;background:#f6f6f4;color:#1d1d1b}
header{padding:20px 28px;background:#fff;border-bottom:1px solid #ddd}
h1{font-size:20px;margin:0 0 4px} .sub{color:#666}
main{padding:20px 28px;max-width:1100px}
.card{background:#fff;border:1px solid #ddd;border-radius:8px;padding:14px 16px;margin:0 0 12px}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600;margin-right:6px}
.confirmed{background:#e3f1e6;color:#1f6b33}.candidate{background:#fff3d6;color:#7a5600}
.exception{background:#e5ecfb;color:#24438f}.red_flag{background:#fbe4e4;color:#8f2424}
.ex{font-weight:600} details{margin-top:8px} table{border-collapse:collapse;width:100%;font-size:12.5px}
td,th{border-top:1px solid #eee;padding:4px 6px;text-align:left;vertical-align:top}
.memo{background:#fafaf8;border-left:3px solid #bbb;padding:6px 10px;margin-top:8px}
.btn{border:1px solid #bbb;background:#fff;border-radius:6px;padding:3px 10px;margin-right:6px;cursor:pointer}
.flag{background:#fbe4e4;border:1px solid #e3b3b3}
"""


def _e(x) -> str:
    return html.escape(str(x))


def write_html(res: dict, path: Path) -> None:
    ev = {e["id"]: e for e in res["evidence"]}
    out = [f"<!doctype html><meta charset='utf-8'><title>Related party review: {_e(res['client'])}</title><style>{CSS}</style>",
           f"<header><h1>Related party completeness review: {_e(res['client'])} ({_e(res['period'])})</h1>"
           f"<div class='sub'>{'SYNTHETIC DATA. ' if res['synthetic_data'] else ''}Report date {_e(res['report_date'])}. "
           f"Checked {res['coverage']['vendors_checked']} vendors; {res['coverage']['vendors_without_pan_or_gstin']} had no PAN or GSTIN (bank details and names only). "
           f"Model: {_e(res['model'])}. The tool reports evidence, not conclusions: absence of a finding means "
           f"'no evidence found in these sources as of this date'.</div></header><main>"]
    for fl in res["document_flags"]:
        out.append(f"<div class='card flag'><b>Document flag:</b> {_e(fl['file'])}: {_e(fl['flag'])} ({_e('; '.join(fl['matches']))}). {_e(fl['handling'])}</div>")
    for f in res["findings"]:
        rows = []
        for s in f["path"]:
            label = s["kind"] + (f" via {s.get('method')} (Tier {s.get('tier')})" if s["kind"] == "same_entity" else f" [{s.get('provenance')}]")
            srcs = "<br>".join(f"<b>{_e(i)}</b> {_e(ev[i]['source'])} {_e(ev[i]['locator'])}: {_e(ev[i]['content'][:140])}"
                               + (" <i>(stale: re-fetch)</i>" if i in f["stale_evidence"] else "") for i in s.get("evidence", []))
            rows.append(f"<tr><td>{_e(label)}</td><td>{_e(s['src'])} &rarr; {_e(s['dst'])}</td><td>{srcs}</td></tr>")
        inv = f["investigation"]
        inv_html = (f"<div><b>Investigator:</b> {_e(inv['status'])} after {inv['tool_calls']} tool calls ({_e(inv['policy'])})</div>"
                    if inv else "")
        out.append(
            f"<div class='card'><span class='badge {f['status']}'>{_e(f['status'].upper())}</span>"
            f"<span class='ex'>{_e(f['exception'].replace('_', ' ').title())}</span><br>"
            f"<b>{_e(f['vendor'])} {_e(f['vendor_name'])}</b> &middot; {_e(f['citation'])}: {_e(f['rule_text'])}<br>"
            f"<span class='sub'>{_e(f['note'])}</span>{inv_html}"
            f"<div class='memo'>{_e(f['memo'])}<br><span class='sub'>memo: {_e(f['memo_source'])}</span></div>"
            f"<details><summary>Evidence trail ({len(f['path'])} steps)</summary><table><tr><th>Step</th><th>Records</th><th>Evidence</th></tr>"
            f"{''.join(rows)}</table></details>"
            f"<div style='margin-top:8px'><button class='btn'>Accept</button><button class='btn'>Reject</button>"
            f"<button class='btn'>Ask client</button><span class='sub'>(mock: decisions are recorded with reviewer name and reason)</span></div></div>")
    out.append("<div class='card'><b>Approval and threshold checks</b><table><tr><th>Party</th><th>Total</th><th>Exceptions</th></tr>")
    for c in res["calculations"]:
        exs = "<br>".join(f"{_e(x['code'])}: {_e(x['detail'])} [{_e(x['citation'])}]" for x in c["exceptions"]) or _e(c.get("exemption", "none"))
        out.append(f"<tr><td>{_e(c['party'])} ({_e(', '.join(c['vendors']))})</td><td>{_e(c['total'])}</td><td>{exs}</td></tr>")
    out.append("</table></div></main>")
    Path(path).write_text("".join(out))
