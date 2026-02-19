import math
import time
import threading
from handlers.utils import safe_float

class AutoCalManager:
    def __init__(self, engine):
        self.engine = engine
        self.config = engine.config
        self.lock = threading.Lock()
        self.need_add_usdt_profit_target = 0.0
        self.need_add_usdt_above_zero = 0.0
        self.need_add_above_zero_per_side = {'long': 0.0, 'short': 0.0}
        self.need_add_profit_target_per_side = {'long': 0.0, 'short': 0.0}
        self.auto_add_step_count = {'long': 0, 'short': 0}
        self.last_add_price = {'long': 0.0, 'short': 0.0}
        self.last_order_time = 0

    def calculate_need_add_metrics(self):
        with self.lock:
            self._calculate_need_add_metrics_internal()

    def _calculate_need_add_metrics_internal(self):
        self.need_add_usdt_profit_target = 0.0
        self.need_add_usdt_above_zero = 0.0
        self.need_add_above_zero_per_side = {'long': 0.0, 'short': 0.0}
        self.need_add_profit_target_per_side = {'long': 0.0, 'short': 0.0}

        # Avoid calculation with default/stale product info
        if not self.engine.product_info.get('is_loaded'):
            if self.engine.monitoring_tick % 60 == 0:
                self.engine.log("Auto-Cal calculation skipped: Product Info not loaded.", level="debug")
            return

        mkt = self.engine.latest_trade_price
        if mkt <= 0:
            # Fallback to entry price to allow calculation even before first ticker
            for side in ['long', 'short']:
                if self.engine.in_position[side]:
                    mkt = self.engine.position_entry_price[side]
                    if mkt > 0: break

        if mkt <= 0: return

        fee_pct = self.config.get('trade_fee_percentage', 0.08) / 100.0
        rec_val = self.config.get('add_pos_recovery_percent', 0.6)
        rec = max(0.1, rec_val) / 100.0
        mult = self.config.get('add_pos_profit_multiplier', 1.5)

        # Gain factor from adding 1 USDT notional and moving 'rec' percent
        # We pay fee_pct for entry and fee_pct for exit (estimated)
        gain_factor = rec - (2 * fee_pct)
        if gain_factor <= 0: gain_factor = 0.0001

        for side in ['long', 'short']:
            if self.engine.in_position[side]:
                notional = self.engine.position_manager.position_notional[side]

                # USER FEEDBACK: Use the same logic as "Net Profit" in Financials.
                # Net Profit = UPL - Current Entry Fees - Realized Loss this cycle
                upl = self.engine.position_manager.position_upl[side]
                entry_fees = self.engine.position_manager.current_entry_fees.get(side, 0.0)
                cycle_loss = self.engine.position_manager.realized_loss_this_cycle.get(side, 0.0)
                current_net_pnl = upl - entry_fees - cycle_loss

                # Simplified formula to ensure Need Add is always positive when in loss/below target.
                # We calculate V such that adding V and moving rec% covers the TARGET profit.
                # V = (Target - CurrentNetPnL) / (rec - 2 * fee_pct)

                # Mode 1: Above Zero (Target Net PnL = 0)
                v_zero = 0.0
                if current_net_pnl < 0:
                    v_zero = (-current_net_pnl) / gain_factor

                if v_zero > 0:
                    self.need_add_above_zero_per_side[side] = v_zero
                    self.need_add_usdt_above_zero += v_zero

                self.engine.log(f"Auto-Cal Debug ({side.upper()}): NetPnL={current_net_pnl:.2f}, UPL={upl:.2f}, Fees={entry_fees:.2f}, Target0_Need={v_zero:.2f}", level="debug")

                # Mode 2: Profit Target
                # Target Net Profit = One-way fee * multiplier
                target_net_profit = (notional * fee_pct) * mult
                v_profit = (target_net_profit - current_net_pnl) / gain_factor
                if v_profit < 0: v_profit = 0.0

                if v_profit > 0:
                    self.need_add_profit_target_per_side[side] = v_profit
                    self.need_add_usdt_profit_target += v_profit
                    self.engine.log(f"Auto-Cal Debug ({side.upper()}): TargetProfit={target_net_profit:.2f}, TargetProfit_Need={v_profit:.2f}", level="debug")

    def check_auto_exit(self, net_pnl, unrealized_pnl):
        notional = self.engine.cached_pos_notional
        if notional <= 0: return False, ""

        fee_pct = self.config.get('trade_fee_percentage', 0.08) / 100.0
        # Use aggregate fees for thresholds
        used_fees = sum(self.engine.position_manager.current_entry_fees.values())
        size_fees = notional * fee_pct

        # 1. Above Zero (Mode 1)
        if self.config.get('use_add_pos_above_zero') and net_pnl >= 0:
            return True, "Above Zero Target Met (Mode 1)"

        # 2. Profit Target (Mode 2)
        if self.config.get('use_add_pos_profit_target'):
            mult = self.config.get('add_pos_profit_multiplier', 1.5)
            # Match user expectation: Net Profit = One-way Fee * Multiplier
            # So Target Unrealized PnL = Total Cycle Fees + Estimated Exit Fee + (One-way Fee * Multiplier)
            one_way_fee = notional * fee_pct
            total_cycle_fees = sum(self.engine.position_manager.current_entry_fees.values())
            target = total_cycle_fees + one_way_fee + (one_way_fee * mult)

            if unrealized_pnl >= target:
                return True, f"Profit Target Met (Mode 2: Net > {one_way_fee * mult:.2f})"

        # 3. Auto-Manual Threshold
        if self.config.get('use_pnl_auto_manual'):
            threshold = self.config.get('pnl_auto_manual_threshold', 100.0)
            if unrealized_pnl >= threshold:
                return True, f"Manual PnL Threshold {threshold} Met"

        # 4. Auto-Cal Profit (Based on Entry Fees)
        if self.config.get('use_pnl_auto_cal'):
            times = self.config.get('pnl_auto_cal_times', 1.2)
            if unrealized_pnl >= (used_fees * times):
                return True, f"Auto-Cal Profit Met ({times}x Entry Fees)"

        # 5. Auto-Cal Loss (Based on Entry Fees)
        if self.config.get('use_pnl_auto_cal_loss'):
            times = self.config.get('pnl_auto_cal_loss_times', 15.0)
            if unrealized_pnl <= -(used_fees * times):
                return True, f"Auto-Cal Loss Met ({times}x Entry Fees)"

        # 6. Size Auto-Cal Profit (Based on Current Notional Fee)
        if self.config.get('use_size_auto_cal'):
            times = self.config.get('size_auto_cal_times', 2.0)
            if unrealized_pnl >= (size_fees * times):
                return True, f"Size Auto-Cal Profit Met ({times}x Size Fees)"

        # 7. Size Auto-Cal Loss (Based on Current Notional Fee)
        if self.config.get('use_size_auto_cal_loss'):
            times = self.config.get('size_auto_cal_loss_times', 1.5)
            if unrealized_pnl <= -(size_fees * times):
                return True, f"Size Auto-Cal Loss Met ({times}x Size Fees)"

        return False, ""

    def check_auto_margin(self):
        if not self.config.get('use_auto_margin'): return
        for side in ['long', 'short']:
            if self.engine.in_position[side]:
                pos = self.engine.position_manager.position_details.get(side, {})
                liqp = self.engine.position_manager.position_liq[side]
                sl = self.engine.current_stop_loss[side]
                if pos.get('mgnMode') == 'isolated' and liqp > 0 and sl > 0:
                    if (side == 'long' and liqp >= sl) or (side == 'short' and liqp <= sl):
                        amt = abs(sl - liqp) + self.config.get('auto_margin_offset', 30.0)
                        self.engine.okx_client.request("POST", "/api/v5/account/position/margin-balance", body_dict={"instId": self.config['symbol'], "posSide": pos.get('posSide', 'net'), "type": "add", "amt": str(round(amt, 2))})

    def check_auto_add(self):
        with self.lock:
            if not any(self.config.get(k) for k in ['use_add_pos_auto_cal', 'use_add_pos_above_zero', 'use_add_pos_profit_target']): return

            # Lockout to prevent rapid-fire adds before position sync
            if time.time() - self.last_order_time < 10: return

            mkt = self.engine.latest_trade_price
            if not mkt: return

            any_in_pos = False
            for side in ['long', 'short']:
                if self.engine.in_position[side]:
                    any_in_pos = True
                    # Robust initialization of last_add_price
                    if self.last_add_price[side] == 0:
                        self.last_add_price[side] = self.engine.position_entry_price[side]
                        if self.last_add_price[side] == 0: continue

                    gap_threshold = float(self.config.get('add_pos_gap_threshold', 5.0))
                    gap_offset = float(self.config.get('add_pos_gap_offset', 0.0))
                    gap = gap_threshold + (self.auto_add_step_count[side] * gap_offset)

                    price_diff = (self.last_add_price[side] - mkt) if side == 'long' else (mkt - self.last_add_price[side])

                    if price_diff >= gap:
                        self.engine.log(f"Auto-Add Gap Triggered: {side} position, last add {self.last_add_price[side]}, mkt {mkt}, gap {gap:.2f}")
                        if self._execute_add(side, mkt):
                            self.last_add_price[side] = mkt
                            break # Only one add per check loop to maintain sanity
                else:
                    self.auto_add_step_count[side] = 0
                    self.last_add_price[side] = 0.0

    def _execute_add(self, side, price):
        # IMPORTANT: Auto-Cal recovery orders bypass budget and min order amount restrictions
        is_recovery = False
        target_notional = 0.0
        if self.config.get('use_add_pos_profit_target') and self.need_add_profit_target_per_side[side] > 0:
            target_notional = max(target_notional, self.need_add_profit_target_per_side[side])
            is_recovery = True
        if self.config.get('use_add_pos_above_zero') and self.need_add_above_zero_per_side[side] > 0:
            target_notional = max(target_notional, self.need_add_above_zero_per_side[side])
            is_recovery = True

        max_adds = int(self.config.get('add_pos_max_count', 10))
        if self.auto_add_step_count[side] >= max_adds:
            self.engine.log(f"Auto-Add: Max steps reached ({self.auto_add_step_count[side]}/{max_adds}). Skipping.", level="info")
            return False

        current_notional = self.engine.position_manager.position_notional[side]
        # Calculate size based on percentage
        pct_base = float(self.config.get('add_pos_size_pct', 5.0))
        pct_offset = float(self.config.get('add_pos_size_pct_offset', 0.0))
        pct = (pct_base + (self.auto_add_step_count[side] * pct_offset)) / 100.0

        sz_pct_notional = current_notional * pct
        final_notional = max(sz_pct_notional, target_notional)

        self.engine.log(f"Auto-Add Calc: Current {current_notional:.2f}, Pct {pct*100:.1f}% -> {sz_pct_notional:.2f}. Recovery Target {target_notional:.2f}. Final {final_notional:.2f}")

        # Auto-Cal Add Position should open trade independent of the Used and Remaining
        self.engine.log("Auto-Cal Add Position: Bypassing budget and min-order restrictions.", level="info")

        contract_multiplier = safe_float(self.engine.product_info.get('contractSize', 1.0))
        sz = final_notional / (price * contract_multiplier)

        # Apply quantity precision and step size
        lot_sz = safe_float(self.engine.product_info.get('qtyStepSize', 1.0))
        sz = round(math.floor(sz / lot_sz) * lot_sz, 8)

        if sz < safe_float(self.engine.product_info.get('minOrderQty', 0)):
            self.engine.log(f"Auto-Add quantity {sz} is below minOrderQty (Target Notional {final_notional:.2f}). Skipping.", level="info")
            return False

        tp, sl = self.engine.order_manager._calculate_tpsl_prices(side, price)

        # Use actual posSide from existing position to maintain consistency
        pos_detail = self.engine.position_manager.position_details.get(side, {})
        actual_pos_side = pos_detail.get('posSide', 'net')

        # Step 2 Exit Offset Override (Relative to New Average Entry)
        step2 = safe_float(self.config.get('add_pos_step2_offset'), 0)
        if step2 > 0:
            p_prec = self.engine.product_info.get('pricePrecision', 2)
            entry = self.engine.position_entry_price[side]
            qty = abs(self.engine.position_qty[side])
            # Estimate new average entry
            new_total_qty = qty + sz
            if new_total_qty > 0:
                new_avg_entry = ((qty * entry) + (sz * price)) / new_total_qty
                if side == 'long': tp = round(new_avg_entry + step2, p_prec)
                else: tp = round(new_avg_entry - step2, p_prec)
                self.engine.log(f"Auto-Add Step 2: New Avg Entry Est {new_avg_entry:.4f}, TP set at {tp:.4f} (Offset {step2})")

        if self.engine.order_manager.place_order(self.config['symbol'], "buy" if side == "long" else "sell", sz,
                                                 order_type="Market", posSide=actual_pos_side, take_profit_price=tp, stop_loss_price=sl,
                                                 context='autocal'):
            self.auto_add_step_count[side] += 1
            self.last_order_time = time.time()
            return True
        return False
