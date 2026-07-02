"""
tools/report_renderer.py -- RCA report rendering
-------------------------------------------------
Renders the HTML / Markdown / console-summary representations of an RCA
report from typed inputs. Pure string-building (render_reports) plus a
small file-writing helper (save_reports). No SQL, no email.
"""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from tools.detection import HeadBlocker
from tools.rca import RCAOutput

SEVERITY_COLOR = {
    "CRITICAL": ("#7f1d1d", "#fee2e2", "#dc2626"),
    "HIGH":     ("#7c2d12", "#ffedd5", "#ea580c"),
    "MEDIUM":   ("#713f12", "#fef9c3", "#ca8a04"),
    "LOW":      ("#14532d", "#dcfce7", "#16a34a"),
}


class ReportInput(BaseModel):
    server_name: str
    head_blocker: HeadBlocker
    decision: str = "SKIP"
    risk_level: str = "LOW"
    rca: RCAOutput
    correlation_id: str = ""
    cycle_start_utc: Optional[str] = None
    dry_run: bool = True
    log_used_mb: float = 0.0
    log_used_pct: float = 0.0
    rule_triggered: int = 0
    kill_status: str = "NOT_ATTEMPTED"
    kill_time_utc: Optional[str] = None
    kill_executed: bool = False
    decision_reason: str = ""
    blocked_texts: list[str] = Field(default_factory=list)


class ReportOutput(BaseModel):
    html: str
    markdown: str
    summary_lines: list[str]


def render_reports(input: ReportInput) -> ReportOutput:
    return ReportOutput(
        html=_render_html(input),
        markdown=_render_markdown(input),
        summary_lines=_render_summary_lines(input),
    )


def save_reports(output: ReportOutput, server_name: str, session_id, decision: str, reports_root: Path) -> tuple[Path, Path]:
    """Write the HTML and Markdown reports to <reports_root>/<server>/, return (html_path, md_path)."""
    ts       = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_srv = re.sub(r'[\\/:*?"<>|]', "_", server_name)
    folder   = reports_root / safe_srv
    folder.mkdir(parents=True, exist_ok=True)

    html_path = folder / f"{ts}_SPID{session_id}_{decision}.html"
    md_path   = folder / f"{ts}_SPID{session_id}_{decision}.md"
    html_path.write_text(output.html, encoding="utf-8")
    md_path.write_text(output.markdown, encoding="utf-8")
    return html_path, md_path


# ── HTML rendering ────────────────────────────────────────────────────────────

def _render_html(input: ReportInput) -> str:
    head = input.head_blocker
    rca  = input.rca
    wait_ms   = head.wait_duration_ms
    wait_sec  = round(wait_ms / 1000, 1)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    sev = (rca.severity or input.risk_level).upper()
    hdr_dark, hdr_light, hdr_accent = SEVERITY_COLOR.get(sev, SEVERITY_COLOR["HIGH"])

    kill_badge = _badge(input.kill_status, "#16a34a" if input.kill_status == "SUCCESS" else "#dc2626")
    dec_badge  = _badge(input.decision,
                         "#1d4ed8" if input.decision == "KILL" else
                         "#d97706" if input.decision == "ALERT_ONLY" else "#6b7280")
    sev_badge  = _badge(sev, hdr_accent)

    rc, bi, recs = rca.root_cause, rca.business_impact, rca.recommendations

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SQL Blocking RCA — {input.server_name} — SPID {head.session_id}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
          background: #f1f5f9; color: #1e293b; line-height: 1.6; font-size: 14px; }}
  .page {{ max-width: 1100px; margin: 32px auto; padding: 0 16px 64px; }}
  .banner {{ background: {hdr_dark}; color: #fff; border-radius: 12px 12px 0 0;
             padding: 28px 36px; display: flex; justify-content: space-between; align-items: flex-start; }}
  .banner h1 {{ font-size: 22px; font-weight: 700; letter-spacing: -0.3px; margin-bottom: 4px; }}
  .banner .sub {{ opacity: .75; font-size: 13px; }}
  .banner-right {{ text-align: right; font-size: 13px; opacity: .85; line-height: 1.8; }}
  .severity-stripe {{ background: {hdr_accent}; color: #fff;
                      padding: 10px 36px; font-size: 13px; font-weight: 600;
                      display: flex; align-items: center; gap: 12px; }}
  .card {{ background: #fff; border: 1px solid #e2e8f0; border-top: none;
           border-radius: 0 0 12px 12px; padding: 36px; }}
  h2 {{ font-size: 15px; font-weight: 700; text-transform: uppercase;
        letter-spacing: .6px; color: {hdr_accent}; margin: 36px 0 14px;
        padding-bottom: 6px; border-bottom: 2px solid {hdr_light}; }}
  h2:first-child {{ margin-top: 0; }}
  h3 {{ font-size: 13px; font-weight: 700; color: #374151; margin: 18px 0 8px; }}
  .summary {{ background: {hdr_light}; border-left: 4px solid {hdr_accent};
              padding: 16px 20px; border-radius: 0 8px 8px 0; font-size: 14px; line-height: 1.7; }}
  .incident-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0;
                    border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden; }}
  .ig-row {{ display: contents; }}
  .ig-row .ig-label, .ig-row .ig-value {{ padding: 10px 16px; border-bottom: 1px solid #e2e8f0; font-size: 13px; }}
  .ig-label {{ background: #f8fafc; font-weight: 600; color: #475569; }}
  .ig-value {{ background: #fff; color: #1e293b; word-break: break-word; }}
  .ig-row:last-child .ig-label, .ig-row:last-child .ig-value {{ border-bottom: none; }}
  .badge {{ display: inline-block; padding: 2px 10px; border-radius: 999px;
            font-size: 11px; font-weight: 700; color: #fff; letter-spacing: .4px; }}
  .rec-section {{ margin-bottom: 24px; }}
  .rec-header {{ display: flex; align-items: center; gap: 10px;
                 background: #f8fafc; border: 1px solid #e2e8f0;
                 border-radius: 8px 8px 0 0; padding: 12px 16px; }}
  .rec-header .rec-title {{ font-size: 13px; font-weight: 700; color: #1e293b; }}
  .rec-header .rec-desc {{ font-size: 12px; color: #64748b; }}
  .rec-items {{ border: 1px solid #e2e8f0; border-top: none; border-radius: 0 0 8px 8px; overflow: hidden; }}
  .rec-item {{ padding: 16px 20px; border-bottom: 1px solid #e2e8f0; }}
  .rec-item:last-child {{ border-bottom: none; }}
  .rec-item-header {{ display: flex; align-items: flex-start; gap: 12px; margin-bottom: 6px; }}
  .rec-num {{ background: {hdr_accent}; color: #fff; border-radius: 50%;
              min-width: 22px; height: 22px; display: flex; align-items: center;
              justify-content: center; font-size: 11px; font-weight: 700; flex-shrink: 0; }}
  .rec-action {{ font-weight: 600; font-size: 13px; color: #1e293b; }}
  .rec-rationale {{ font-size: 12px; color: #64748b; margin: 4px 0 0 34px; line-height: 1.6; }}
  pre.sql {{ background: #0f172a; color: #e2e8f0; padding: 14px 18px; border-radius: 6px;
             font-family: 'Cascadia Code', 'Fira Code', Consolas, monospace;
             font-size: 12px; line-height: 1.6; overflow-x: auto;
             margin: 10px 0 0 34px; white-space: pre-wrap; word-break: break-word; }}
  .p1 {{ background: #dc2626; }} .p2 {{ background: #ea580c; }}
  .p3 {{ background: #2563eb; }} .p4 {{ background: #7c3aed; }}
  .footer {{ margin-top: 40px; padding-top: 20px; border-top: 1px solid #e2e8f0;
             font-size: 12px; color: #94a3b8; text-align: center; }}
</style>
</head>
<body>
<div class="page">
  <div class="banner">
    <div>
      <div style="font-size:11px;opacity:.6;margin-bottom:6px;letter-spacing:.8px;">SQL SERVER BLOCKING INCIDENT</div>
      <h1>Root Cause Analysis &amp; Recommendation Report</h1>
      <div class="sub">Correlation ID: {input.correlation_id or 'N/A'} &nbsp;|&nbsp;
        Generated: {generated}</div>
    </div>
    <div class="banner-right">{dec_badge} {kill_badge}<br>Server: <strong>{input.server_name}</strong><br>SPID: <strong>{head.session_id}</strong></div>
  </div>
  <div class="severity-stripe">{sev_badge} &nbsp;{_esc(rca.severity_justification)}</div>
  <div class="card">
    <h2>Executive Summary</h2>
    <div class="summary">{_esc(rca.executive_summary)}</div>
    <h2>Incident Details</h2>
    <div class="incident-grid">
      {_ig('Incident Time (UTC)', str(input.cycle_start_utc or 'N/A')[:19].replace('T',' '))}
      {_ig('Server', input.server_name)}
      {_ig('Head Blocker SPID', str(head.session_id))}
      {_ig('Blocking Login', head.login_name or 'N/A')}
      {_ig('Wait Duration', f"{wait_ms:,} ms &nbsp;({wait_sec} s)")}
      {_ig('Sessions Blocked', str(bi.affected_sessions or head.victim_count))}
      {_ig('Blocking Chain', head.blocking_chain or 'N/A')}
      {_ig('Decision', dec_badge)}
      {_ig('Kill Status', kill_badge)}
      {_ig('Dry Run', str(input.dry_run))}
      {_ig('Transaction Log Used', f"{input.log_used_mb:.1f} MB &nbsp;({input.log_used_pct:.1f}%)")}
      {_ig('Rule Triggered', str(input.rule_triggered))}
    </div>
    <h2>Blocking SQL (Head Blocker)</h2>
    <pre class="sql">{_esc(head.sql_text[:1200] or 'N/A')}</pre>
    <h2>Blocked Sessions (Victims)</h2>
    {_victim_sqls(input.blocked_texts)}
    <h2>Root Cause</h2>
    <h3>{_esc(rc.headline)}</h3>
    <p style="color:#374151;line-height:1.75;white-space:pre-wrap">{_esc(rc.detail)}</p>
    <h2>Business Impact</h2>
    <p style="color:#374151;line-height:1.75;white-space:pre-wrap">{_esc(bi.impact_description)}</p>
    <h2>Recommendations</h2>
    {_rec_section("P1","p1","Immediate Actions","Do today — stop the bleeding and notify the right people",recs.immediate)}
    {_rec_section("P2","p2","Short-Term Fixes","This week — code, config, and schema changes to prevent recurrence",recs.short_term)}
    {_rec_section("P3","p3","Long-Term Preventive Measures","This quarter — architectural and process improvements",recs.long_term)}
    {_rec_section("P4","p4","Monitoring & Alerting Improvements","DMV queries and Extended Events to detect this pattern earlier",recs.monitoring)}
    <h2>Agent Decision Rationale</h2>
    <div class="summary" style="border-color:#94a3b8;background:#f8fafc">{_esc(input.decision_reason or 'N/A')}</div>
    <div class="footer">Generated by SQL Blocking Agent &nbsp;|&nbsp;
      {generated} &nbsp;|&nbsp;
      Correlation ID: {input.correlation_id or 'N/A'}</div>
  </div>
</div>
</body>
</html>"""
    return html


def _render_markdown(input: ReportInput) -> str:
    head = input.head_blocker
    header = (
        f"# SQL Server Blocking Incident — RCA Report\n\n"
        f"| Field | Value |\n|---|---|\n"
        f"| Server | {input.server_name} |\n"
        f"| Generated | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} |\n"
        f"| Correlation ID | {input.correlation_id or 'N/A'} |\n"
        f"| SPID | {head.session_id} |\n"
        f"| Login | {head.login_name} |\n"
        f"| Decision | {input.decision} |\n"
        f"| Risk | {input.risk_level} |\n"
        f"| Kill Status | {input.kill_status} |\n\n---\n\n"
    )
    return header + input.rca.to_text()


def _render_summary_lines(input: ReportInput) -> list[str]:
    head, rca, recs = input.head_blocker, input.rca, input.rca.recommendations
    sep = "=" * 70
    lines = [
        sep,
        "SQL BLOCKING AGENT  --  RCA & RECOMMENDATION REPORT",
        f"Server:      {input.server_name}",
        f"Decision:    {input.decision}  |  Risk: {input.risk_level}  |  Dry Run: {input.dry_run}",
        f"Blocker:     SPID {head.session_id}  login={head.login_name}  wait={head.wait_duration_ms} ms  victims={head.victim_count}",
    ]
    if input.kill_executed:
        lines.append(f"Kill status: {input.kill_status}  at {input.kill_time_utc}")
    lines.append("")
    lines.append(f"ROOT CAUSE:  {rca.root_cause.headline}")
    lines.append(f"SEVERITY:    {rca.severity}  --  {rca.severity_justification}")
    lines.append("")
    lines.append("RECOMMENDATIONS SUMMARY:")
    for label, items in [("P1 Immediate", recs.immediate), ("P2 Short-term", recs.short_term),
                          ("P3 Long-term", recs.long_term), ("P4 Monitoring", recs.monitoring)]:
        for i, item in enumerate(items, 1):
            lines.append(f"  [{label}.{i}] {item.action}")
    lines.append(sep)
    lines.append("")
    return lines


# ── HTML helpers ───────────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
                     .replace(">", "&gt;").replace('"', "&quot;"))


def _badge(label: str, color: str) -> str:
    return f'<span class="badge" style="background:{color}">{_esc(label)}</span>'


def _ig(label: str, value: str) -> str:
    return (f'<div class="ig-row"><div class="ig-label">{label}</div>'
            f'<div class="ig-value">{value}</div></div>')


def _victim_sqls(blocked_texts: list[str]) -> str:
    if not blocked_texts:
        return '<p style="color:#64748b;font-size:13px;font-style:italic">No victim SQL captured.</p>'
    parts = []
    for i, sql in enumerate(blocked_texts, 1):
        parts.append(
            f'<div class="rec-item" style="margin-bottom:8px">'
            f'<div class="rec-item-header">'
            f'<div class="rec-num">{i}</div>'
            f'<div class="rec-action">Blocked Session #{i}</div></div>'
            f'<pre class="sql" style="margin:6px 0 0 34px">{_esc(sql[:800])}</pre></div>'
        )
    return '\n'.join(parts)


def _rec_section(priority_label, css_cls, title, description, items) -> str:
    if not items:
        return ""
    badge = f'<span class="badge {css_cls}">{priority_label}</span>'
    rows = ""
    for i, item in enumerate(items, 1):
        action = _esc(item.action)
        rationale = _esc(item.rationale)
        sql = item.sql or ""
        sql_block = f'<pre class="sql">{_esc(sql.strip())}</pre>' if sql.strip() else ""
        rows += (f'<div class="rec-item"><div class="rec-item-header">'
                 f'<div class="rec-num">{i}</div><div class="rec-action">{action}</div></div>'
                 f'<div class="rec-rationale">{rationale}</div>{sql_block}</div>')
    return (f'<div class="rec-section"><div class="rec-header">{badge}'
            f'<div><div class="rec-title">{title}</div>'
            f'<div class="rec-desc">{description}</div></div></div>'
            f'<div class="rec-items">{rows}</div></div>')
