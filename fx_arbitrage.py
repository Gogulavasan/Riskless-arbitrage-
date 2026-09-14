"""
FX Arbitrage Intelligence Engine  —  Covered Interest Rate Parity (CIRP) module
================================================================================

Tests COVERED interest rate parity only.  Never Uncovered IRP: every rate here is a
contractual forward, not an expected future spot, so no exchange-rate risk is taken.

Quoting convention (CFA Level 1, "price/base")
----------------------------------------------
A pair is written BASE/QUOTE and a rate S means "S units of QUOTE per 1 unit of BASE".
    EUR/USD = 1.1600  ->  1 EUR costs 1.16 USD  ->  base = EUR, quote = USD
In the CFA formula the QUOTE (price) currency is the "domestic" currency, so:

    F = S x (1 + i_domestic * tau_d) / (1 + i_foreign * tau_f)          [CFA-L1 tested]
        i_domestic = i_quote,  i_foreign = i_base,  tau = days / basis

    Mispricing               = F_market - F_fair                          [CFA-L1 tested]
    Forward premium/discount = F / S - 1                                  [CFA-L1 tested]
    Gross arbitrage profit   = Final amount - Amount owed
    Net profit               = Gross arbitrage profit - Trading costs

Day-count note (a place the curriculum simplifies): CFA L1 uses a single tau = days/360
for both legs.  Real money markets use ACT/360 for USD, EUR, JPY and ACT/365 for GBP, INR.
The engine takes the basis per currency; set both to 360 to reproduce the textbook.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ----------------------------------------------------------------------------
# 1. Inputs
# ----------------------------------------------------------------------------


@dataclass
class MarketInputs:
    pair: str                 # "EUR/USD" -> base EUR, quote USD
    days: int                 # tenor in calendar days (3M ~ 91)
    spot_mid: float           # S, quote per base
    fwd_mid: float            # F_market, outright forward, quote per base
    i_base: float             # annualised risk-free rate of BASE currency (decimal)
    i_quote: float            # annualised risk-free rate of QUOTE currency (decimal)
    basis_base: int = 360     # day-count basis for base-currency rate (360 or 365)
    basis_quote: int = 360    # day-count basis for quote-currency rate
    note: str = ""            # where the numbers came from

    @property
    def base(self) -> str:
        return self.pair.split("/")[0]

    @property
    def quote(self) -> str:
        return self.pair.split("/")[1]

    @property
    def tau_base(self) -> float:
        return self.days / self.basis_base

    @property
    def tau_quote(self) -> float:
        return self.days / self.basis_quote


@dataclass
class CostInputs:
    """Real-world frictions.  All spreads are FULL bid-ask width; the engine uses half
    on each side of mid.  Rates are annualised decimals, pips are in price units."""
    spot_spread: float = 0.0        # in price units (e.g. 0.0001 = 1 pip on EUR/USD)
    fwd_spread: float = 0.0         # in price units, on the outright forward
    borrow_spread: float = 0.0      # you borrow at i + borrow_spread (annualised)
    deposit_spread: float = 0.0     # you invest at i - deposit_spread (annualised)
    margin_pct: float = 0.0         # initial margin on the forward, fraction of notional
    margin_funding: float = 0.0     # annualised cost of funding that margin (decimal)
    fee_bps: float = 0.0            # flat brokerage / settlement fee, bps of notional


# ----------------------------------------------------------------------------
# 2. Core CIRP maths
# ----------------------------------------------------------------------------


def implied_forward(m: MarketInputs) -> float:
    """F_fair = S x (1 + i_quote*tau_q) / (1 + i_base*tau_b)."""
    return m.spot_mid * (1 + m.i_quote * m.tau_quote) / (1 + m.i_base * m.tau_base)


def forward_premium(f: float, s: float) -> float:
    """Unannualised premium (+) / discount (-) on the BASE currency: F/S - 1."""
    return f / s - 1


def annualise_bps(x: float, days: int, basis: int = 360) -> float:
    """Turn a per-period fraction into annualised basis points (simple, ACT/basis)."""
    return x * basis / days * 10_000


@dataclass
class ParityResult:
    f_fair: float
    f_market: float
    mispricing: float           # F_market - F_fair (price units)
    mispricing_bps_ann: float   # relative gap, annualised, bps
    premium_market: float       # F_mkt/S - 1
    premium_fair: float         # F_fair/S - 1
    direction: str              # 'borrow_quote' | 'borrow_base' | 'none'
    flags: List[str] = field(default_factory=list)


def analyse_parity(m: MarketInputs, tol_bps: float = 0.01) -> ParityResult:
    f_fair = implied_forward(m)
    gap = m.fwd_mid - f_fair
    gap_bps = annualise_bps(gap / m.spot_mid, m.days, m.basis_quote)

    flags: List[str] = []

    # --- Sanity check 1: parity DIRECTION.  Higher-rate currency must trade at a
    # forward discount.  If i_base > i_quote then F_fair < S, so F_market should be < S.
    if m.i_base > m.i_quote and m.fwd_mid > m.spot_mid:
        flags.append(
            f"INCONSISTENT INPUT: {m.base} has the HIGHER rate but trades at a forward "
            f"PREMIUM (F_mkt {m.fwd_mid:.6g} > S {m.spot_mid:.6g}). Parity says it must be "
            f"at a discount. Check the forward quote / rate inputs before trusting output."
        )
    if m.i_base < m.i_quote and m.fwd_mid < m.spot_mid:
        flags.append(
            f"INCONSISTENT INPUT: {m.base} has the LOWER rate but trades at a forward "
            f"DISCOUNT (F_mkt {m.fwd_mid:.6g} < S {m.spot_mid:.6g}). Parity says premium."
        )

    # --- Sanity check 2: size.  In liquid G10 pairs a mid-to-mid gap above ~25 bp
    # annualised almost always means stale/mismatched data, not free money.
    if abs(gap_bps) > 25:
        flags.append(
            f"RED FLAG: annualised mispricing of {gap_bps:+.1f} bp is far larger than what "
            f"survives in liquid FX. Suspect mismatched tenors, wrong day-count, stale spot, "
            f"or (for INR-type pairs) capital controls / onshore-offshore segmentation."
        )

    if abs(gap_bps) <= tol_bps:
        direction = "none"
    elif gap > 0:
        direction = "borrow_quote"   # forward sells base too dear -> sell base fwd
    else:
        direction = "borrow_base"    # forward sells base too cheap -> buy base fwd

    return ParityResult(
        f_fair=f_fair, f_market=m.fwd_mid, mispricing=gap, mispricing_bps_ann=gap_bps,
        premium_market=forward_premium(m.fwd_mid, m.spot_mid),
        premium_fair=forward_premium(f_fair, m.spot_mid),
        direction=direction, flags=flags,
    )


# ----------------------------------------------------------------------------
# 3. Executing the trade with frictions  (Profit = Final amount - Amount owed)
# ----------------------------------------------------------------------------


def trade_pnl(m: MarketInputs, c: CostInputs, direction: str, notional: float = 1.0,
              use_spot_spread=True, use_fwd_spread=True, use_rate_spread=True,
              use_margin=True, use_fees=True) -> Dict[str, float]:
    """
    Run the full covered-interest-arbitrage round trip for 1 unit of the BORROWED
    currency and return {'final','owed','gross','margin_cost','fees','net'} in that currency.

    borrow_quote:  borrow Q -> buy B at spot ASK -> deposit B -> sell B fwd at fwd BID -> repay Q
    borrow_base :  borrow B -> sell B at spot BID -> deposit Q -> buy B fwd at fwd ASK -> repay B
    Each 'use_*' switch turns one friction on/off so the waterfall can attribute erosion.
    """
    hs = c.spot_spread / 2 if use_spot_spread else 0.0
    hf = c.fwd_spread / 2 if use_fwd_spread else 0.0
    bs = c.borrow_spread if use_rate_spread else 0.0
    ds = c.deposit_spread if use_rate_spread else 0.0

    spot_bid, spot_ask = m.spot_mid - hs, m.spot_mid + hs
    fwd_bid, fwd_ask = m.fwd_mid - hf, m.fwd_mid + hf

    if direction == "borrow_quote":
        i_pay = notional * (m.i_quote + bs) * m.tau_quote
        owed = notional + i_pay
        base_bought = notional / spot_ask
        i_earn = base_bought * (m.i_base - ds) * m.tau_base * fwd_bid   # converted at fwd
        fx_principal = base_bought * fwd_bid - notional                # principal round trip
        final = base_bought * (1 + (m.i_base - ds) * m.tau_base) * fwd_bid
        tau = m.tau_quote
    elif direction == "borrow_base":
        i_pay = notional * (m.i_base + bs) * m.tau_base
        owed = notional + i_pay
        quote_received = notional * spot_bid
        i_earn = quote_received * (m.i_quote - ds) * m.tau_quote / fwd_ask
        fx_principal = quote_received / fwd_ask - notional
        final = quote_received * (1 + (m.i_quote - ds) * m.tau_quote) / fwd_ask
        tau = m.tau_base
    else:
        return dict(final=notional, owed=notional, gross=0.0, margin_cost=0.0, fees=0.0,
                    net=0.0, i_earn=0.0, i_pay=0.0, fx_principal=0.0)

    gross = final - owed          # == fx_principal + i_earn - i_pay  (identity)
    margin_cost = notional * c.margin_pct * c.margin_funding * tau if use_margin else 0.0
    fees = notional * c.fee_bps / 10_000 if use_fees else 0.0
    net = gross - margin_cost - fees
    return dict(final=final, owed=owed, gross=gross, margin_cost=margin_cost, fees=fees,
                net=net, i_earn=i_earn, i_pay=i_pay, fx_principal=fx_principal)


def cost_waterfall(m: MarketInputs, c: CostInputs, direction: str) -> List[tuple]:
    """Sequentially switch frictions on and record how much each one erodes the profit.
    Returns [(label, cumulative_net_per_unit), ...] — first row is the frictionless gross."""
    steps = []
    kw = dict(use_spot_spread=False, use_fwd_spread=False, use_rate_spread=False,
              use_margin=False, use_fees=False)
    steps.append(("Gross arbitrage profit (mid, risk-free)", trade_pnl(m, c, direction, **kw)["net"]))
    for key, label in [("use_spot_spread", "less spot bid-ask"),
                       ("use_fwd_spread", "less forward bid-ask"),
                       ("use_rate_spread", "less borrow/lend spread"),
                       ("use_margin", "less forward margin funding"),
                       ("use_fees", "less brokerage / settlement fees")]:
        kw[key] = True
        steps.append((label, trade_pnl(m, c, direction, **kw)["net"]))
    return steps


# ----------------------------------------------------------------------------
# 4. Country tax layer  —  ILLUSTRATIVE PLACEHOLDERS, NOT TAX ADVICE
# ----------------------------------------------------------------------------
#
# A CIA round trip has three taxable pieces, and jurisdictions do NOT treat them alike:
#     interest earned on the deposit leg      (income)
#     interest paid on the borrowing leg      (deductible?  not always)
#     gain/loss on principal from spot->forward (FX / derivative result; a LOSS here is
#                                              often 'ring-fenced' and cannot offset interest)
# Taxing the pieces separately is what lets a marginally profitable trade turn into an
# after-tax loss.  Every rate below is a placeholder showing the *shape* of a regime.

TAX_DISCLAIMER = ("Tax rates are illustrative placeholders (shape of each regime only). "
                  "Verify current law before relying on any after-tax figure.")


@dataclass
class TaxTreatment:
    jurisdiction: str
    label: str
    rate_income: float          # on net interest income
    rate_fx: float              # on a positive FX / forward result
    interest_deductible: bool   # can borrowing interest (+ fees, margin funding) offset income?
    cross_offset: bool  # may a loss in one pool shelter a gain in the other?
    notes: str


def tax_treatments(country: str, entity: str = "individual") -> List[TaxTreatment]:
    """Return one or more candidate treatments (ambiguous regimes return two)."""
    country, entity = country.lower(), entity.lower()
    t: List[TaxTreatment] = []
    if country == "india":
        if entity == "company":
            t.append(TaxTreatment("India", "Company – business income (s.115BAA-style 25.17%), forward result pooled",
                0.2517, 0.2517, True, True,
                "For a company whose business is trading, forward gains/losses are business income; "
                "s.43(5) proviso (d)/(e) keeps eligible currency derivatives non-speculative. Placeholder."))
        else:
            who = "LLP" if entity == "llp" else "Individual"
            t.append(TaxTreatment("India", f"{who} – interest at slab 31.2%; forward loss treated as SPECULATIVE (s.43(5)) and ring-fenced",
                0.312, 0.312, True, False,
                "If the OTC forward is a non-delivery 'speculative transaction' under s.43(5), its loss can "
                "only be set off against speculative profits (s.73) — NOT against the deposit interest. "
                "That asymmetry is what can push the trade after-tax negative. If instead the forward is "
                "an eligible hedge/exchange-traded contract, it pools with business income. Placeholder."))
            t.append(TaxTreatment("India", f"{who} – business income pooled at 31.2% (forward NOT speculative)",
                0.312, 0.312, True, True, "Symmetric case for contrast. Placeholder."))
    elif country in ("us", "usa", "united states"):
        t.append(TaxTreatment("US", "§988 – everything ordinary @37%: interest and FX result pooled",
            0.37, 0.37, True, True,
            "Default for FX gains/losses; ordinary loss offsets interest income. Placeholder."))
        t.append(TaxTreatment("US", "§1256 – forward is 60/40 capital (26.8%); interest ordinary @37%; capital LOSS ring-fenced",
            0.37, 0.268, True, False,
            "A §1256 forward loss is a capital loss — only $3k/yr usable against ordinary interest "
            "income. So on a trade whose profit comes from the interest leg, the forward loss is "
            "trapped and interest is taxed in full. Contract type ambiguous -> both shown. NIIT excluded."))
    elif country in ("europe", "eu", "germany", "de"):
        t.append(TaxTreatment("Europe (generic, Germany-style)", "Abgeltungsteuer 26.375% on interest; derivative (Termingeschäft) LOSS ring-fenced",
            0.26375, 0.26375, False, False,
            "No single EU regime. Germany-style placeholder: flat 25% + 5.5% soli on capital income, "
            "borrowing costs for private investors NOT deductible, and forward-contract losses "
            "offsettable only against other derivative gains (capped). Specify the member state."))
        t.append(TaxTreatment("Europe (generic, Germany-style)", "Business-income framework – everything pooled @ ~30%",
            0.30, 0.30, True, True, "Corporate/trading-entity contrast case. Placeholder."))
    elif country in ("south korea", "korea", "kr"):
        t.append(TaxTreatment("South Korea", "Interest withheld at 15.4% (below global-aggregation threshold); OTC forward result outside the net",
            0.154, 0.11, False, False,
            "Interest income is withheld at source (14% + 1.4% local) and a private investor cannot "
            "deduct borrowing cost or offset a derivative loss against it. Derivative gains: separate "
            "flat regime (~11% placeholder, may not cover OTC forwards). Placeholder."))
        t.append(TaxTreatment("South Korea", "Above threshold – interest aggregated at 49.5% top marginal; forward result still separate",
            0.495, 0.11, False, False,
            "Global-financial-income aggregation (KRW 20m placeholder) pushes interest to progressive "
            "rates up to 45% + 10% local. Placeholder."))
    elif country in ("australia", "au"):
        t.append(TaxTreatment("Australia", "Div 775 forex measures – forex gain/loss ORDINARY income @47%, pooled with interest",
            0.47, 0.47, True, True,
            "Under Div 775 ITAA 1997 a forex realisation gain/loss is assessable/deductible as ORDINARY "
            "income — not CGT — so no 50% discount even at >12m, but also no capital-loss quarantine. "
            "Company placeholder 30%. Verify."))
        t.append(TaxTreatment("Australia", "Contrast: CGT-style @47%, capital loss quarantined against capital gains only",
            0.47, 0.47, True, False,
            "Shown only to make the Div 775 vs CGT distinction visible: the quarantine would hurt here."))
    else:
        raise ValueError(f"Unknown jurisdiction '{country}'. Ask before adding new ones.")
    return t


def tax_due(pnl: Dict[str, float], tr: TaxTreatment) -> float:
    """Tax on one round trip, in the borrowed currency.  Interest pool and FX pool are taxed
    separately; whether a loss in one pool can shelter the other depends on the regime."""
    deductions = pnl["i_pay"] + pnl["margin_cost"] + pnl["fees"] if tr.interest_deductible else 0.0
    income_pool = pnl["i_earn"] - deductions
    fx_pool = pnl["fx_principal"]
    if tr.cross_offset:
        if fx_pool >= 0 and income_pool >= 0:
            return tr.rate_income * income_pool + tr.rate_fx * fx_pool
        pooled = income_pool + fx_pool
        rate = tr.rate_income if fx_pool < 0 else tr.rate_fx   # loss reduces the other pool
        return rate * max(pooled, 0.0)
    return tr.rate_income * max(income_pool, 0.0) + tr.rate_fx * max(fx_pool, 0.0)


def after_tax(pnl: Dict[str, float], tr: TaxTreatment) -> float:
    return pnl["net"] - tax_due(pnl, tr)


# ----------------------------------------------------------------------------
# 5. Verdict + report
# ----------------------------------------------------------------------------


def verdict(gross: float, net: float, tol: float = 1e-9) -> str:
    if abs(gross) <= tol:
        return "No arbitrage"
    if net > tol:
        return "Arbitrage survives costs"
    return "Arbitrage exists but is cost-negative"


def run(m: MarketInputs, c: CostInputs, country: str, entity: str = "individual",
        notional: float = 1_000_000.0) -> Dict:
    p = analyse_parity(m)
    d = p.direction if p.direction != "none" else "borrow_quote"  # compute anyway for display
    wf = cost_waterfall(m, c, d)
    full = trade_pnl(m, c, d)
    gross_pu, net_pu = wf[0][1], wf[-1][1]
    borrowed_ccy = m.quote if d == "borrow_quote" else m.base
    treatments = tax_treatments(country, entity)
    tax_rows = [(t, tax_due(full, t), after_tax(full, t)) for t in treatments]
    def vt(a):
        if net_pu <= 1e-9:
            return "Cost-negative before tax"
        return "Profitable after tax" if a > 1e-9 else "TAX FLIPS IT TO A LOSS"
    return dict(m=m, c=c, parity=p, direction=d, waterfall=wf, full=full,
                gross_pu=gross_pu, net_pu=net_pu, borrowed_ccy=borrowed_ccy,
                notional=notional, tax=tax_rows,
                verdict_cost=verdict(gross_pu, net_pu),
                verdict_tax=[(t.label, vt(a)) for t, _, a in tax_rows])


def fmt_price(x: float, pair: str) -> str:
    return f"{x:.3f}" if pair.startswith("USD/JPY") else f"{x:.5f}" if pair.endswith("INR") else f"{x:.6f}"


def report(r: Dict) -> str:
    m, p, c = r["m"], r["parity"], r["c"]
    N = r["notional"]
    ccy = r["borrowed_ccy"]
    L = []
    L.append("=" * 78)
    L.append(f"{m.pair}   {m.days}-day tenor   ({m.note})")
    L.append("=" * 78)
    L.append("STEP 1  Inputs")
    L.append(f"  S (spot mid)            = {fmt_price(m.spot_mid, m.pair)}  {m.quote} per {m.base}")
    L.append(f"  F_market (fwd mid)      = {fmt_price(m.fwd_mid, m.pair)}")
    L.append(f"  i_{m.base} (foreign/base)   = {m.i_base:.4%}  ACT/{m.basis_base}  tau = {m.tau_base:.6f}")
    L.append(f"  i_{m.quote} (domestic/quote) = {m.i_quote:.4%}  ACT/{m.basis_quote}  tau = {m.tau_quote:.6f}")
    L.append("STEP 2  CIRP implied forward  [CFA L1: F = S(1+i_d tau)/(1+i_f tau)]")
    L.append(f"  F_fair = {fmt_price(m.spot_mid, m.pair)} x (1+{m.i_quote:.4%}x{m.tau_quote:.6f}) / (1+{m.i_base:.4%}x{m.tau_base:.6f})")
    L.append(f"         = {fmt_price(p.f_fair, m.pair)}")
    L.append("STEP 3  Compare  [CFA L1: forward premium/discount = F/S - 1]")
    L.append(f"  Premium/discount on {m.base}: market {p.premium_market:+.5%}   fair {p.premium_fair:+.5%}")
    L.append(f"  Mispricing = F_market - F_fair = {p.mispricing:+.6f}  ({p.mispricing_bps_ann:+.2f} bp annualised)")
    L.append("STEP 4  Theoretical direction  [CFA L1: no-arbitrage principle]")
    if p.direction == "none":
        L.append("  None — market forward equals CIRP forward within tolerance.")
    elif p.direction == "borrow_quote":
        L.append(f"  Forward sells {m.base} too DEAR -> borrow {m.quote}, buy {m.base} spot, invest {m.base}, SELL {m.base} forward.")
    else:
        L.append(f"  Forward sells {m.base} too CHEAP -> borrow {m.base}, sell {m.base} spot, invest {m.quote}, BUY {m.base} forward.")
    L.append(f"STEP 5  Cost waterfall  (per {N:,.0f} {ccy} borrowed;  Profit = Final - Owed)")
    prev = None
    for label, v in r["waterfall"]:
        delta = "" if prev is None else f"   ({(v - prev) * N:+,.2f})"
        L.append(f"  {label:<42s} {v * N:>14,.2f} {ccy}{delta}")
        prev = v
    f = r["full"]
    L.append(f"  Final amount {f['final'] * N:,.2f}  -  Amount owed {f['owed'] * N:,.2f}  = gross {f['gross'] * N:,.2f};  "
             f"net after margin+fees {f['net'] * N:,.2f}")
    L.append(f"  Net, annualised on notional: {annualise_bps(r['net_pu'], m.days, m.basis_quote if r['direction']=='borrow_quote' else m.basis_base):+.2f} bp")
    L.append(f"STEP 6  Verdict after costs:  >>> {r['verdict_cost']} <<<")
    L.append("STEP 6b Tax layer  (" + TAX_DISCLAIMER + ")")
    L.append(f"  Taxable pieces: interest earned {f['i_earn'] * N:,.2f} | interest paid {f['i_pay'] * N:,.2f} | "
             f"margin+fees {(f['margin_cost'] + f['fees']) * N:,.2f} | FX result on principal {f['fx_principal'] * N:+,.2f}")
    for (t, tx, a), (_, vt) in zip(r["tax"], r["verdict_tax"]):
        L.append(f"  {t.label}")
        L.append(f"     tax {tx * N:>12,.2f}   after-tax {a * N:>14,.2f} {ccy}   -> {vt}")
        if vt == "TAX FLIPS IT TO A LOSS":
            L.append("     ** INSIGHT: the regime taxes the profitable leg but ring-fences the loss-making leg — "
                     "a pre-tax profit becomes an after-tax loss. **")
    L.append("STEP 7  Sanity checks")
    if p.flags:
        for fl in p.flags:
            L.append("  ! " + fl)
    else:
        L.append("  Parity direction consistent; gap within the range seen in liquid markets.")
    return "\n".join(L)


# ----------------------------------------------------------------------------
# 6. Worked hypothetical  (labelled — see build_examples)
# ----------------------------------------------------------------------------

def build_examples() -> List[tuple]:
    """Illustrative Sep-2026 levels: spot from published month-open levels, 3M money-market
    rates set a few bp around policy rates (Fed 3.75, ECB 2.25, BoE 3.75, BoJ 1.00, RBI 5.25).
    Market forwards are CONSTRUCTED = fair forward + a deliberate mispricing so each case
    lands in a different verdict bucket.  Not live quotes."""
    ex = []
    # Two cost profiles.  "bank": a dealer funding near the risk-free curve with CSA-
    # collateralised forwards (the only participant for whom CIA is realistically live).
    # "retail": a non-bank paying real credit spreads and posting margin.
    bank = CostInputs(spot_spread=0.00005, fwd_spread=0.0001, borrow_spread=0.0005,
                      deposit_spread=0.0002, margin_pct=0.0, margin_funding=0.0, fee_bps=0.1)
    g10 = CostInputs(spot_spread=0.0001, fwd_spread=0.0002, borrow_spread=0.0025,
                     deposit_spread=0.0010, margin_pct=0.02, margin_funding=0.04, fee_bps=0.5)
    jpy = CostInputs(spot_spread=0.01, fwd_spread=0.02, borrow_spread=0.0025,
                     deposit_spread=0.0010, margin_pct=0.02, margin_funding=0.04, fee_bps=0.5)
    inr = CostInputs(spot_spread=0.01, fwd_spread=0.03, borrow_spread=0.0100,
                     deposit_spread=0.0050, margin_pct=0.05, margin_funding=0.065, fee_bps=2.0)

    e = MarketInputs("EUR/USD", 91, 1.1600, 0.0, 0.0220, 0.0380, 360, 360, "hypothetical, fair +3 pips, bank cost profile")
    e.fwd_mid = round(implied_forward(e) + 0.0003, 6)
    ex.append((e, bank, "US", "individual"))

    g = MarketInputs("GBP/USD", 91, 1.3500, 0.0, 0.0370, 0.0380, 365, 360, "hypothetical, exactly at parity")
    g.fwd_mid = implied_forward(g)
    ex.append((g, g10, "Europe", "individual"))

    j = MarketInputs("USD/JPY", 91, 160.00, 0.0, 0.0380, 0.0105, 360, 360, "hypothetical, fair -0.03")
    j.fwd_mid = round(implied_forward(j) - 0.03, 4)
    ex.append((j, jpy, "South Korea", "individual"))

    i = MarketInputs("USD/INR", 91, 94.90, 0.0, 0.0380, 0.0540, 360, 365, "hypothetical, fair +0.25 (onshore/NDF segmentation)")
    i.fwd_mid = round(implied_forward(i) + 0.25, 4)
    ex.append((i, inr, "India", "individual"))
    return ex


if __name__ == "__main__":
    for m, c, country, entity in build_examples():
        print(report(run(m, c, country, entity)))
        print()
