/* Live FX arbitrage monitor — client side.
   Live spot from a public rate feed; interest rates and forward points from monitor_config.json
   (overridable per browser in Settings); maths from engine.js; alerts via the Notification API. */
(function () {
  const $ = id => document.getElementById(id), F = FX.fmt;
  const LS_KEY = "arb-monitor-settings-v1";
  let CFG = null, SET = null, timer = null, countdown = null, lastSpot = null, lastSpotMeta = null;
  let alertState = {};          // pair -> boolean (was alerting last tick)
  let log = [];                 // session alert log
  let serverSignals = null;

  // ---------- settings (config + per-browser overrides)
  function loadSettings() {
    let s = null; try { s = JSON.parse(localStorage.getItem(LS_KEY) || "null"); } catch {}
    const base = { tenor_days: CFG.tenor_days, poll_seconds: CFG.poll_seconds, alert_threshold_bp: CFG.alert_threshold_bp,
      quotes_are_real: CFG.quotes_are_real, quotes_as_of: CFG.quotes_as_of,
      rates: JSON.parse(JSON.stringify(CFG.rates)), pairs: JSON.parse(JSON.stringify(CFG.pairs)) };
    if (s && s.v === 1) { Object.assign(base, s.data); }
    SET = base;
  }
  function saveSettings() { try { localStorage.setItem(LS_KEY, JSON.stringify({ v: 1, data: SET })); } catch {} }
  function resetSettings() { try { localStorage.removeItem(LS_KEY); } catch {} loadSettings(); renderSettings(); compute(); }

  // ---------- spot feed
  async function fetchSpot() {
    for (const url of CFG.spot_sources) {
      try {
        const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), 8000);
        const r = await fetch(url, { signal: ctl.signal, cache: "no-store" }); clearTimeout(t);
        if (!r.ok) continue;
        const j = await r.json();
        const rates = j.rates || (j.conversion_rates); if (!rates || !rates.EUR) continue;
        const when = j.date ? new Date(j.date) : (j.time_last_update_utc ? new Date(j.time_last_update_utc) : new Date());
        return { rates, when, source: new URL(url).hostname };
      } catch (e) { /* try next */ }
    }
    return null;
  }
  function spotFor(pair, usdBase) { const [b, q] = pair.split("/"); return q === "USD" ? 1 / usdBase[b] : usdBase[q]; }

  // ---------- compute one tick
  function evalPair(p, usdBase) {
    const [b, q] = p.pair.split("/");
    const S = spotFor(p.pair, usdBase);
    const rb = SET.rates[b], rq = SET.rates[q];
    const m = { pair: p.pair, days: SET.tenor_days, spot: S, fwd: S + p.forward_points, ib: rb.rate / 100, iq: rq.rate / 100, bb: rb.basis, bq: rq.basis };
    const c = CFG.costs[p.cost_profile] || CFG.costs.bank;
    const r = FX.run(m, c, 1e6);
    const opp = r.netbp > SET.alert_threshold_bp && r.a.flags.length === 0;
    const suspect = r.a.flags.length > 0;
    return { p, m, r, S, opp, suspect, fair: r.a.f, mkt: m.fwd, gapbp: r.a.bps, netbp: r.netbp, dir: r.dir, ccy: r.ccy };
  }

  function compute() {
    const usdBase = lastSpot || CFG.sample_spot_usd_base;
    const rows = SET.pairs.map(p => evalPair(p, usdBase));
    renderTable(rows); renderKpis(rows); handleAlerts(rows);
    return rows;
  }

  // ---------- rendering
  const px = (x, pair) => x.toFixed(pair.endsWith("JPY") ? 3 : pair.endsWith("INR") ? 4 : 5);
  function statusPill(row) {
    if (row.suspect) return '<span class="pill warn">check inputs</span>';
    if (row.opp) return '<span class="pill good">opportunity</span>';
    if (row.gapbp === 0 || Math.abs(row.gapbp) < 0.01) return '<span class="pill neu">at fair value</span>';
    return '<span class="pill neu">no edge after costs</span>';
  }
  function tradeText(row) {
    if (Math.abs(row.gapbp) < 0.5) return "—";
    const [b, q] = row.p.pair.split("/");
    return row.dir === "borrow_quote" ? `Borrow ${q}, buy ${b}, sell ${b} forward` : `Borrow ${b}, sell ${b}, buy ${b} forward`;
  }
  function renderTable(rows) {
    let h = `<tr><th>Pair</th><th>Live spot</th><th>Forward points</th><th>Market forward</th><th>Fair forward</th><th>Gap</th><th>After costs</th><th>Status</th><th style="text-align:left">Trade</th></tr>`;
    for (const x of rows) {
      const cls = x.netbp > 0 ? "good" : (x.netbp < 0 ? "bad" : "");
      h += `<tr class="${x.opp ? "opp" : ""}"><td><b>${x.p.pair}</b><br><span class="note">${(CFG.costs[x.p.cost_profile] || CFG.costs.bank).name}</span></td>
        <td class="num">${px(x.S, x.p.pair)}</td>
        <td class="num">${F.sgn(x.p.forward_points)}</td>
        <td class="num">${px(x.mkt, x.p.pair)}</td>
        <td class="num">${px(x.fair, x.p.pair)}</td>
        <td class="num">${F.sgn(x.gapbp.toFixed(2))} bp</td>
        <td class="num ${cls}"><b>${F.sgn(x.netbp.toFixed(2))} bp</b></td>
        <td>${statusPill(x)}</td>
        <td style="text-align:left;font-size:12px">${tradeText(x)}</td></tr>`;
    }
    $("tbl").innerHTML = h;
  }
  function renderKpis(rows) {
    const opps = rows.filter(x => x.opp), best = rows.reduce((a, b) => (b.suspect ? a : (a === null || b.netbp > a.netbp ? b : a)), null);
    const fromServer = lastSpotMeta && lastSpotMeta.source === "server check";
    const meta = lastSpotMeta ? `${lastSpotMeta.source} · ${lastSpotMeta.when.toLocaleTimeString()}` : "sample spot (feed unreachable)";
    $("kpis").innerHTML = [
      ["Pairs watched", rows.length, `${SET.tenor_days}-day forwards`, ""],
      ["Opportunities now", opps.length, opps.length ? opps.map(x => x.p.pair).join(", ") : "none above threshold", opps.length ? "good" : ""],
      ["Best after costs", best ? `${F.sgn(best.netbp.toFixed(2))} bp` : "—", best ? `${best.p.pair} · threshold ${SET.alert_threshold_bp} bp` : "", best && best.netbp > 0 ? "good" : ""],
      ["Spot feed", !lastSpot ? "sample" : (fromServer ? "last server check" : "live"), meta, !lastSpot ? "warn" : (fromServer ? "" : "good")],
    ].map(([l, v, s, c]) => `<div class="tile ${c}"><div class="lab">${l}</div><div class="val">${v}</div><div class="sub">${s}</div></div>`).join("");
    $("quoteBadge").innerHTML = SET.quotes_are_real
      ? `<span class="pill info">broker quotes as of ${SET.quotes_as_of}</span>`
      : `<span class="pill warn">sample forward points — replace with your broker's quotes in Settings</span>`;
  }
  function renderLog() {
    const items = [...log].reverse().slice(0, 50);
    const server = serverSignals && serverSignals.alerts ? serverSignals.alerts : [];
    $("log").innerHTML = (items.length || server.length) ? "" : '<p class="note">No alerts yet this session. Alerts appear here when a pair\'s after-cost gap crosses the threshold.</p>';
    if (server.length) $("log").innerHTML += `<p class="note">Server check at ${new Date(serverSignals.checked_at).toLocaleString()} found: ${server.map(a => `${a.pair} ${F.sgn(a.net_bp.toFixed(2))} bp`).join(", ")}</p>`;
    $("log").innerHTML += items.map(e => `<div class="logrow"><span class="num">${e.t.toLocaleTimeString()}</span><span>${e.msg}</span></div>`).join("");
  }

  // ---------- alerts
  let audioCtx = null;
  function beep() { try { audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)(); const o = audioCtx.createOscillator(), g = audioCtx.createGain(); o.connect(g); g.connect(audioCtx.destination); o.frequency.value = 880; g.gain.value = 0.08; o.start(); o.stop(audioCtx.currentTime + 0.25); } catch {} }
  function notify(title, body) {
    if ("Notification" in window && Notification.permission === "granted") { try { new Notification(title, { body, tag: "arb-" + title }); } catch {} }
  }
  function handleAlerts(rows) {
    const banner = $("banner"); const active = rows.filter(x => x.opp);
    if (active.length) {
      banner.hidden = false;
      banner.innerHTML = `<b>${active.length === 1 ? "Opportunity" : active.length + " opportunities"}</b> — ` + active.map(x => `${x.p.pair}: ${F.sgn(x.netbp.toFixed(2))} bp per year after costs · ${tradeText(x)}`).join(" · ") +
        (SET.quotes_are_real ? "" : ` <span class="pill warn">based on sample quotes</span>`);
    } else { banner.hidden = true; }
    for (const x of rows) {
      const was = !!alertState[x.p.pair];
      if (x.opp && !was) {
        const msg = `${x.p.pair}: ${F.sgn(x.netbp.toFixed(2))} bp after costs — ${tradeText(x)} (spot ${px(x.S, x.p.pair)}, gap ${F.sgn(x.gapbp.toFixed(2))} bp)`;
        log.push({ t: new Date(), msg });
        if (SET.quotes_are_real) { notify(`FX arbitrage: ${x.p.pair}`, msg); beep(); }
      }
      if (!x.opp && was) log.push({ t: new Date(), msg: `${x.p.pair}: opportunity closed (${F.sgn(x.netbp.toFixed(2))} bp)` });
      alertState[x.p.pair] = x.opp;
    }
    renderLog();
  }

  // ---------- settings UI
  function renderSettings() {
    const rates = Object.entries(SET.rates).map(([ccy, r]) => `
      <div class="row"><label>${ccy} interest rate, % per year<small>${r.source || ""}</small></label><input type="number" step="0.01" data-rate="${ccy}" value="${r.rate}"></div>`).join("");
    const pairs = SET.pairs.map((p, i) => `
      <div class="row"><label>${p.pair} forward points<small>market forward minus spot, ${SET.tenor_days}-day, price units</small></label><input type="number" step="any" data-pts="${i}" value="${p.forward_points}"></div>`).join("");
    $("settingsBody").innerHTML = `
      <div class="grid2">
        <div><h2>Interest rates</h2>${rates}
          <div class="row"><label>Tenor (days)</label><input type="number" data-k="tenor_days" value="${SET.tenor_days}"></div></div>
        <div><h2>Your forward quotes</h2>${pairs}
          <div class="row"><label>These are real broker quotes<small>Turns on browser notifications and sound. Leave off while using sample values.</small></label><select data-k="quotes_are_real"><option value="false" ${!SET.quotes_are_real ? "selected" : ""}>No — sample values</option><option value="true" ${SET.quotes_are_real ? "selected" : ""}>Yes — real quotes</option></select></div>
          <div class="row"><label>Quotes as of</label><input type="text" data-k="quotes_as_of" value="${SET.quotes_as_of}"></div>
          <div class="row"><label>Alert threshold, bp per year after costs</label><input type="number" step="0.5" data-k="alert_threshold_bp" value="${SET.alert_threshold_bp}"></div>
          <div class="row"><label>Refresh spot every (seconds)</label><input type="number" min="30" step="30" data-k="poll_seconds" value="${SET.poll_seconds}"></div></div>
      </div>
      <p style="margin:12px 0 0;display:flex;gap:8px;flex-wrap:wrap"><button class="primary" id="saveBtn">Save and re-check</button><button id="resetBtn">Reset to site defaults</button></p>
      <p class="note">Settings are saved in this browser only. To change what the server-side checker uses (and what everyone sees), edit <code>monitor_config.json</code> in the GitHub repo.</p>`;
    $("saveBtn").onclick = () => {
      document.querySelectorAll("#settingsBody [data-rate]").forEach(el => { SET.rates[el.dataset.rate].rate = parseFloat(el.value); });
      document.querySelectorAll("#settingsBody [data-pts]").forEach(el => { SET.pairs[+el.dataset.pts].forward_points = parseFloat(el.value); });
      document.querySelectorAll("#settingsBody [data-k]").forEach(el => { const k = el.dataset.k; SET[k] = k === "quotes_are_real" ? el.value === "true" : (k === "quotes_as_of" ? el.value : parseFloat(el.value)); });
      saveSettings(); restartTimer(); compute(); $("settings").open = false;
    };
    $("resetBtn").onclick = resetSettings;
  }

  // ---------- timer
  let secsLeft = 0;
  function restartTimer() {
    clearInterval(timer); clearInterval(countdown);
    timer = setInterval(tick, Math.max(30, SET.poll_seconds) * 1000);
    secsLeft = Math.max(30, SET.poll_seconds);
    countdown = setInterval(() => { secsLeft = Math.max(0, secsLeft - 1); $("next").textContent = secsLeft; }, 1000);
  }
  async function tick() {
    $("feedStatus").textContent = "refreshing…";
    const s = await fetchSpot();
    if (s) { lastSpot = s.rates; lastSpotMeta = s; $("feedStatus").textContent = `spot updated ${s.when.toLocaleTimeString()} from ${s.source}`; }
    else { $("feedStatus").textContent = lastSpot ? "feed unreachable — showing last known spot" : "feed unreachable — showing sample spot"; }
    secsLeft = Math.max(30, SET.poll_seconds);
    compute();
  }

  // ---------- boot
  async function boot() {
    CFG = await (await fetch("monitor_config.json", { cache: "no-store" })).json();
    loadSettings(); renderSettings();
    try { serverSignals = await (await fetch("signals.json", { cache: "no-store" })).json(); } catch {}
    if (serverSignals && serverSignals.spot_usd_base && !lastSpot) { lastSpot = serverSignals.spot_usd_base; lastSpotMeta = { source: "server check", when: new Date(serverSignals.checked_at) }; }
    $("notifyBtn").onclick = async () => {
      if (!("Notification" in window)) { $("notifyBtn").textContent = "Notifications not supported"; return; }
      const p = await Notification.requestPermission();
      $("notifyBtn").textContent = p === "granted" ? "Browser alerts on" : "Alerts blocked in browser";
      if (p === "granted") notify("Arbitrage monitor", "Alerts are on. Keep this tab open.");
    };
    if ("Notification" in window && Notification.permission === "granted") $("notifyBtn").textContent = "Browser alerts on";
    $("refreshBtn").onclick = tick;
    await tick(); restartTimer();
  }
  boot();
})();
