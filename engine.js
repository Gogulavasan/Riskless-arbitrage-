/* FX Arbitrage Intelligence Engine — browser port of fx_arbitrage.py (same maths, cross-checked).
   Pair BASE/QUOTE: S = units of QUOTE per 1 BASE.
   F_fair = S (1 + i_quote τ_q) / (1 + i_base τ_b);  mispricing = F_market − F_fair;  premium = F/S − 1;
   Profit = Final amount − Amount owed;  Net = Gross − Trading costs. */
(function (global) {
  // key, jurisdiction, entity, label, rate_income, rate_fx, borrow_deductible, pools_offset, notes
  const REGIMES = [
    ["IN-ind-spec", "India", "individual", "Individual – interest at slab 31.2%; forward loss SPECULATIVE (s.43(5)), ring-fenced", 0.312, 0.312, true, false, "If the OTC forward is a non-delivery 'speculative transaction' under s.43(5), its loss can only be set off against speculative profits (s.73) — not against deposit interest."],
    ["IN-ind-pool", "India", "individual", "Individual – business income pooled at 31.2% (forward not speculative)", 0.312, 0.312, true, true, "Symmetric case: eligible hedge / exchange-traded contract pools with business income."],
    ["IN-com", "India", "company", "Company – business income (s.115BAA-style 25.17%), forward result pooled", 0.2517, 0.2517, true, true, "Trading company: forward gains/losses are business income; s.43(5) proviso keeps eligible currency derivatives non-speculative."],
    ["US-988", "United States", "individual", "§988 – everything ordinary @37%, pooled", 0.37, 0.37, true, true, "Default for FX gains/losses; ordinary loss offsets interest income."],
    ["US-1256", "United States", "individual", "§1256 – forward 60/40 capital (26.8%); interest ordinary @37%; capital LOSS ring-fenced", 0.37, 0.268, true, false, "A §1256 forward loss is capital — only $3k/yr usable against ordinary income. Contract type often ambiguous → both shown. NIIT excluded."],
    ["US-corp", "United States", "company", "C-corp – 21% federal, everything pooled", 0.21, 0.21, true, true, "State tax excluded. Placeholder."],
    ["EU-abg", "Europe (Germany-style)", "individual", "Abgeltungsteuer 26.375%; derivative LOSS ring-fenced; borrowing cost not deductible", 0.26375, 0.26375, false, false, "No single EU regime. Germany-style: 25% + 5.5% soli, private borrowing cost not deductible, forward-contract losses offset only against derivative gains (capped). Specify the member state."],
    ["EU-biz", "Europe (Germany-style)", "company", "Corporate – everything pooled @ ~30%", 0.30, 0.30, true, true, "Corporate / trading-entity contrast case."],
    ["KR-wht", "South Korea", "individual", "Interest withheld 15.4% (below global-aggregation threshold); OTC forward outside the net", 0.154, 0.11, false, false, "Interest withheld at source (14% + 1.4% local); private investor cannot deduct borrowing cost or offset a derivative loss."],
    ["KR-agg", "South Korea", "individual", "Above threshold – interest aggregated at 49.5%; forward still separate", 0.495, 0.11, false, false, "Global-financial-income aggregation (KRW 20m placeholder) pushes interest to progressive rates."],
    ["KR-corp", "South Korea", "company", "Corporate – pooled @ ~24% (incl. local)", 0.24, 0.24, true, true, "Placeholder."],
    ["AU-775", "Australia", "individual", "Div 775 forex measures – ORDINARY income @47%, pooled", 0.47, 0.47, true, true, "Div 775 ITAA 1997: forex realisation gain/loss is ordinary income — no 50% CGT discount even >12m, but no capital-loss quarantine either."],
    ["AU-cgt", "Australia", "individual", "Contrast: CGT-style @47%, capital loss quarantined", 0.47, 0.47, true, false, "Shown only to make the Div 775 vs CGT distinction visible."],
    ["AU-corp", "Australia", "company", "Company – 30%, pooled", 0.30, 0.30, true, true, "Base-rate entity 25% if eligible. Placeholder."],
  ];
  const COUNTRIES = ["India", "United States", "Europe (Germany-style)", "South Korea", "Australia"];

  const COSTS = {
    bank:   { name: "Bank / dealer",   ss: 0.00005, fs: 0.0001, bs: 0.0005, ds: 0.0002, mp: 0,    mf: 0,     fee: 0.1 },
    retail: { name: "Retail / non-bank", ss: 0.0001, fs: 0.0002, bs: 0.0025, ds: 0.001,  mp: 0.02, mf: 0.04,  fee: 0.5 },
    jpy:    { name: "Retail (JPY quoted)", ss: 0.01, fs: 0.02, bs: 0.0025, ds: 0.001, mp: 0.02, mf: 0.04, fee: 0.5 },
    inr:    { name: "Onshore INR",     ss: 0.01,   fs: 0.03,   bs: 0.01,   ds: 0.005,  mp: 0.05, mf: 0.065, fee: 2 },
  };
  // Illustrative Sep-2026 levels; market forwards set at fair + a deliberate gap.
  const PAIRS = [
    { pair: "EUR/USD", days: 91, spot: 1.16, fwd: 1.164966,           ib: 0.022, iq: 0.038,  bb: 360, bq: 360, cost: "bank",   note: "fair +3 pips, bank cost profile" },
    { pair: "GBP/USD", days: 91, spot: 1.35, fwd: 1.3505095122527264, ib: 0.037, iq: 0.038,  bb: 365, bq: 360, cost: "retail", note: "exactly at fair value" },
    { pair: "USD/JPY", days: 91, spot: 160,  fwd: 158.8684,           ib: 0.038, iq: 0.0105, bb: 360, bq: 360, cost: "jpy",    note: "fair −0.03" },
    { pair: "USD/INR", days: 91, spot: 94.9, fwd: 95.5126,            ib: 0.038, iq: 0.054,  bb: 360, bq: 365, cost: "inr",    note: "fair +0.25 (onshore market gap)" },
  ];

  function prep(m) { const o = { ...m }; [o.base, o.quote] = o.pair.split("/"); o.tb = o.days / o.bb; o.tq = o.days / o.bq; return o; }
  const impliedForward = m => m.spot * (1 + m.iq * m.tq) / (1 + m.ib * m.tb);
  const annBps = (x, days, basis) => x * basis / days * 1e4;

  function analyse(m) {
    const f = impliedForward(m), gap = m.fwd - f, bps = annBps(gap / m.spot, m.days, m.bq), flags = [];
    if (m.ib > m.iq && m.fwd > m.spot) flags.push(["hot", `Check your inputs: ${m.base} has the higher interest rate, so its forward rate (${m.fwd}) should be below the spot rate (${m.spot}), not above it. One of the numbers is probably wrong.`]);
    if (m.ib < m.iq && m.fwd < m.spot) flags.push(["hot", `Check your inputs: ${m.base} has the lower interest rate, so its forward rate (${m.fwd}) should be above the spot rate (${m.spot}), not below it.`]);
    if (Math.abs(bps) > 25) flags.push(["warn", `Looks too good: a gap of ${bps.toFixed(1)} bp per year is far bigger than real markets allow. Usually this means stale or mismatched data, or a currency with capital controls (like INR) — not free money.`]);
    return { f, gap, bps, pm: m.fwd / m.spot - 1, pf: f / m.spot - 1, dir: Math.abs(bps) <= 0.01 ? "none" : (gap > 0 ? "borrow_quote" : "borrow_base"), flags };
  }

  function tradePnl(m, c, dir, N, on) {
    on = on || { s: 1, f: 1, r: 1, mg: 1, fe: 1 };
    const hs = on.s ? c.ss / 2 : 0, hf = on.f ? c.fs / 2 : 0, bs = on.r ? c.bs : 0, ds = on.r ? c.ds : 0;
    const sbid = m.spot - hs, sask = m.spot + hs, fbid = m.fwd - hf, fask = m.fwd + hf; const r = {};
    if (dir === "borrow_quote") { r.i_pay = N * (m.iq + bs) * m.tq; r.owed = N + r.i_pay; const b = N / sask; r.i_earn = b * (m.ib - ds) * m.tb * fbid; r.fx = b * fbid - N; r.final = b * (1 + (m.ib - ds) * m.tb) * fbid; r.tau = m.tq; }
    else { r.i_pay = N * (m.ib + bs) * m.tb; r.owed = N + r.i_pay; const q = N * sbid; r.i_earn = q * (m.iq - ds) * m.tq / fask; r.fx = q / fask - N; r.final = q * (1 + (m.iq - ds) * m.tq) / fask; r.tau = m.tb; }
    r.gross = r.final - r.owed; r.margin = on.mg ? N * c.mp * c.mf * r.tau : 0; r.fees = on.fe ? N * c.fee / 1e4 : 0; r.net = r.gross - r.margin - r.fees; return r;
  }
  const WF = [["Gross arbitrage profit (mid, risk-free)", {}], ["less spot bid-ask", { s: 1 }], ["less forward bid-ask", { s: 1, f: 1 }], ["less borrow/lend spread", { s: 1, f: 1, r: 1 }], ["less forward margin funding", { s: 1, f: 1, r: 1, mg: 1 }], ["less brokerage / settlement fees", { s: 1, f: 1, r: 1, mg: 1, fe: 1 }]];
  const waterfall = (m, c, dir, N) => WF.map(([l, on]) => [l, tradePnl(m, c, dir, N, on).net]);

  function taxDue(p, t) {
    const ri = t[4], rf = t[5], ded = t[6], off = t[7];
    const ip = p.i_earn - (ded ? p.i_pay + p.margin + p.fees : 0), fp = p.fx;
    if (off) { if (fp >= 0 && ip >= 0) return ri * ip + rf * fp; return (fp < 0 ? ri : rf) * Math.max(ip + fp, 0); }
    return ri * Math.max(ip, 0) + rf * Math.max(fp, 0);
  }
  const verdictCost = (gross, net) => Math.abs(gross) <= 1e-6 ? "No arbitrage" : (net > 1e-6 ? "Arbitrage survives costs" : "Arbitrage exists but is cost-negative");
  const verdictTax = (net, at) => net <= 1e-6 ? "Cost-negative before tax" : (at > 1e-6 ? "Profitable after tax" : "TAX FLIPS IT TO A LOSS");

  function run(mIn, c, N) {
    const m = prep(mIn), a = analyse(m), dir = a.dir === "none" ? "borrow_quote" : a.dir;
    const wf = waterfall(m, c, dir, N), full = tradePnl(m, c, dir, N);
    const ccy = dir === "borrow_quote" ? m.quote : m.base;
    const netbp = annBps(full.net / N, m.days, dir === "borrow_quote" ? m.bq : m.bb);
    return { m, a, dir, wf, full, ccy, gross: wf[0][1], net: full.net, netbp, verdict: verdictCost(wf[0][1], full.net) };
  }

  const fmt = {
    n2: x => x.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
    n0: x => x.toLocaleString(undefined, { maximumFractionDigits: 0 }),
    px: (x, pair) => x.toFixed(pair.endsWith("JPY") ? 3 : 6),
    pct: (x, d = 4) => (x * 100).toFixed(d) + "%",
    sgn: x => (x >= 0 ? "+" : "") + x,
  };

  global.FX = { REGIMES, COUNTRIES, COSTS, PAIRS, prep, impliedForward, annBps, analyse, tradePnl, waterfall, taxDue, verdictCost, verdictTax, run, fmt, N_DEFAULT: 1e6 };
})(window);
