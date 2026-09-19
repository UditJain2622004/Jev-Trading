from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from jev_trading.binance.types import D, Liquidity, Side


# Conservative VIP0 Spot defaults. Demo keys can replace these via account/commission.
VIP0 = Decimal("0.001")  # 10 bps maker and taker
VIP0_BNB = Decimal("0.00075")  # 25% BNB discount


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    maker: Decimal
    taker: Decimal
    label: str = "custom"

    def rate(self, liquidity: Liquidity) -> Decimal:
        return self.maker if liquidity is Liquidity.MAKER else self.taker

    @classmethod
    def vip0(cls, *, use_bnb_discount: bool = False) -> FeeSchedule:
        rate = VIP0_BNB if use_bnb_discount else VIP0
        label = "vip0_bnb" if use_bnb_discount else "vip0"
        return cls(maker=rate, taker=rate, label=label)

    @classmethod
    def from_account_commission(cls, payload: dict[str, object], *, apply_bnb_discount: bool | None = None) -> FeeSchedule:
        """Build a schedule from GET /api/v3/account/commission."""
        standard = payload["standardCommission"]  # type: ignore[index]
        maker = D(standard["maker"]) + D(standard.get("buyer", "0"))
        taker = D(standard["taker"]) + D(standard.get("buyer", "0"))
        discount = payload.get("discount") or {}
        enabled = bool(discount.get("enabledForAccount") and discount.get("enabledForSymbol"))
        if apply_bnb_discount is None:
            apply_bnb_discount = enabled
        if apply_bnb_discount:
            factor = Decimal(1) - D(discount.get("discount", "0.25"))
            maker *= factor
            taker *= factor
        return cls(maker=maker, taker=taker, label="account")


def commission_from_received(
    received: Decimal,
    rate: Decimal,
    *,
    precision: int = 8,
) -> Decimal:
    """Binance takes commission from the asset you receive.

    BUY receives base. SELL receives quote. We round half-up to the asset
    precision so paper results stay stable and slightly conservative.
    """
    quantum = Decimal("1").scaleb(-precision)
    return (received * rate).quantize(quantum, rounding=ROUND_HALF_UP)


def apply_receive_fee(
    side: Side,
    base_qty: Decimal,
    quote_qty: Decimal,
    rate: Decimal,
    *,
    base_asset: str,
    quote_asset: str,
    base_precision: int = 8,
    quote_precision: int = 8,
) -> tuple[Decimal, Decimal, Decimal, str]:
    """Return (base_delta, quote_delta, fee_amount, fee_asset) after commission.

    Deltas are signed from the account's point of view.
    """
    if side is Side.BUY:
        fee = commission_from_received(base_qty, rate, precision=base_precision)
        return base_qty - fee, -quote_qty, fee, base_asset
    fee = commission_from_received(quote_qty, rate, precision=quote_precision)
    return -base_qty, quote_qty - fee, fee, quote_asset


@dataclass(frozen=True, slots=True)
class RoundTripEconomics:
    notional: Decimal
    entry_price: Decimal
    target_net: Decimal
    buy_fee: Decimal
    sell_fee: Decimal
    slippage_cost: Decimal
    total_cost: Decimal
    required_gross: Decimal
    required_move_bps: Decimal
    required_exit_price: Decimal
    buy_rate: Decimal
    sell_rate: Decimal
    extra_slippage_bps_each_way: Decimal

    @property
    def fees_alone_bps(self) -> Decimal:
        if self.notional == 0:
            return Decimal(0)
        return ((self.buy_fee + self.sell_fee) / self.notional) * Decimal(10000)

    @property
    def viable_vs_one_tick(self) -> bool:
        return self.required_move_bps > 0


def round_trip_economics(
    notional: Decimal | str | float = 1000,
    entry_price: Decimal | str | float = 1,
    target_net: Decimal | str | float = 1,
    *,
    buy_liquidity: Liquidity = Liquidity.TAKER,
    sell_liquidity: Liquidity = Liquidity.TAKER,
    fees: FeeSchedule | None = None,
    extra_slippage_bps_each_way: Decimal | str | float = 1,
) -> RoundTripEconomics:
    """How far price must move to net `target_net` after fees and slippage.

    For a $1000 BTCUSDT clip at VIP0 taker/taker:
    round-trip fees are $2. One extra bp of slippage each way is $0.20.
    Netting $1 therefore needs about 32 bps of favorable move, not $1 of raw
    price change.
    """
    schedule = fees or FeeSchedule.vip0()
    notion = D(notional)
    entry = D(entry_price)
    target = D(target_net)
    buy_rate = schedule.rate(buy_liquidity)
    sell_rate = schedule.rate(sell_liquidity)
    buy_fee = notion * buy_rate
    # Sell notional is close to entry notional; using entry is a tight estimate.
    sell_fee = notion * sell_rate
    slip = D(extra_slippage_bps_each_way)
    slippage_cost = notion * (slip * 2) / Decimal(10000)
    total_cost = buy_fee + sell_fee + slippage_cost
    required_gross = total_cost + target
    required_move_bps = (required_gross / notion) * Decimal(10000) if notion else Decimal(0)
    required_exit = entry * (Decimal(1) + required_move_bps / Decimal(10000))
    return RoundTripEconomics(
        notional=notion,
        entry_price=entry,
        target_net=target,
        buy_fee=buy_fee,
        sell_fee=sell_fee,
        slippage_cost=slippage_cost,
        total_cost=total_cost,
        required_gross=required_gross,
        required_move_bps=required_move_bps,
        required_exit_price=required_exit,
        buy_rate=buy_rate,
        sell_rate=sell_rate,
        extra_slippage_bps_each_way=slip,
    )


def net_pnl_quote(
    entry_price: Decimal,
    exit_price: Decimal,
    base_qty: Decimal,
    *,
    fees: FeeSchedule | None = None,
    extra_slippage_bps_each_way: Decimal = Decimal(0),
    buy_liquidity: Liquidity = Liquidity.TAKER,
    sell_liquidity: Liquidity = Liquidity.TAKER,
) -> Decimal:
    """Approximate long round-trip PnL in quote, after fees and extra slippage."""
    schedule = fees or FeeSchedule.vip0()
    buy_price = entry_price * (Decimal(1) + D(extra_slippage_bps_each_way) / Decimal(10000))
    sell_price = exit_price * (Decimal(1) - D(extra_slippage_bps_each_way) / Decimal(10000))
    buy_notional = buy_price * base_qty
    sell_notional = sell_price * base_qty
    buy_fee = buy_notional * schedule.rate(buy_liquidity)
    sell_fee = sell_notional * schedule.rate(sell_liquidity)
    # BUY fee is taken in base, which is economically buy_fee in quote at entry.
    return sell_notional - buy_notional - buy_fee - sell_fee
