"""Spread instruments must be priced as the NET of both legs, not a single leg.

Regression for the insta-stop bug: pricing 'NIFTY 23850/23750 PE spread' as one
leg (~63) over-marked the spread vs its entry net-credit (~17) and 2× stop (~34),
firing the SELL stop on the first poll at a phantom max-loss.
"""
import core.dhan_data as dd


def test_spread_legs_parse():
    assert dd._spread_legs("NIFTY 23850/23750 PE spread") == (23850.0, 23750.0, "PE")
    assert dd._spread_legs("NIFTY 24100/24200 CE spread") == (24100.0, 24200.0, "CE")
    assert dd._spread_legs("NIFTY 23800 PE") is None          # single leg, not a spread
    assert dd._spread_legs("BANKNIFTY FUT") is None


def test_spread_priced_as_net(monkeypatch):
    monkeypatch.setattr(dd, "_option_scrip",
                        lambda u, k, ot, expiry=None: f"NSE_FNO:{int(k)}{ot}")
    monkeypatch.setattr(dd, "get_ltp",
                        lambda s, codes: {"NSE_FNO:23850PE": 80.0, "NSE_FNO:23750PE": 63.0})
    lookup = dd.make_price_lookup(session=object())
    call = {"category": "index_option", "underlying": "NIFTY",
            "instrument": "NIFTY 23850/23750 PE spread", "option_expiry": None}
    # net = 80 - 63 = 17 (the true cost-to-close), NOT 63/80 (a single leg)
    assert lookup(call) == 17.0


def test_single_leg_unchanged(monkeypatch):
    monkeypatch.setattr(dd, "resolve_scrip_for_call", lambda c: "NSE_FNO:23800PE")
    monkeypatch.setattr(dd, "get_ltp", lambda s, codes: {"NSE_FNO:23800PE": 55.0})
    lookup = dd.make_price_lookup(session=object())
    call = {"category": "index_option", "underlying": "NIFTY", "instrument": "NIFTY 23800 PE"}
    assert lookup(call) == 55.0
