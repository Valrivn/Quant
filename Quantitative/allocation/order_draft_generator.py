from typing import Dict, List, Any, Optional

from Quantitative.sensitivity.sensitivity_vector import SensitivityEngine, SensitivityVector


class OrderDraftGenerator:
    def generate_order_draft(
        self,
        current_holdings: Dict[str, float],
        target_allocations: Dict[str, float],
        portfolio_value: float,
        etf_prices: Dict[str, float],
        sensitivity_vectors: Optional[Dict[str, SensitivityVector]] = None,
        risk_tolerance: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """Generate BUY/SELL orders from target vs current allocations.

        Args:
            current_holdings: Current allocation percentages per ETF
            target_allocations: Desired allocation percentages per ETF
            portfolio_value: Total portfolio value in USD
            etf_prices: Current price per ETF
            sensitivity_vectors: Optional S-vectors for sensitivity-adjusted allocation
            risk_tolerance: 0.0 = no sensitivity adjustment, 1.0 = maximum tilt

        Returns:
            List of order dicts with action, shares, and allocation details
        """
        # Apply sensitivity adjustment if vectors and tolerance provided
        adjusted_allocations = dict(target_allocations)
        if sensitivity_vectors and risk_tolerance > 0.0:
            engine = SensitivityEngine()
            adjusted_allocations = engine.adjust_allocations(
                base_alloc=target_allocations,
                vectors=sensitivity_vectors,
                risk_tolerance=risk_tolerance,
            )

        orders = []
        all_etfs = set(current_holdings.keys()) | set(adjusted_allocations.keys())
        for etf in sorted(list(all_etfs)):
            curr_pct = current_holdings.get(etf, 0.0)
            tgt_pct = adjusted_allocations.get(etf, 0.0)
            orig_tgt = target_allocations.get(etf, 0.0)
            delta = (tgt_pct - curr_pct) * portfolio_value

            if abs(delta) > 0.01:
                price = etf_prices.get(etf, 0.0)
                if price > 0:
                    shares = round(delta / price, 3)
                    if shares != 0:
                        order = {
                            "ETF": etf,
                            "Action": "BUY" if shares > 0 else "SELL",
                            "Shares": abs(shares),
                            "Target_Allocation_Pct": f"{tgt_pct:.2%}",
                            "Current_Allocation_Pct": f"{curr_pct:.2%}",
                            "Target_Value": f"${portfolio_value * tgt_pct:,.2f}",
                            "Current_Value": f"${portfolio_value * curr_pct:,.2f}",
                            "Delta_Value": f"${delta:,.2f}",
                        }

                        # Attach sensitivity metadata if available
                        if sensitivity_vectors and etf in sensitivity_vectors:
                            vec = sensitivity_vectors[etf]
                            order["sensitivity"] = {
                                "composite": round(vec.composite, 4),
                                "label": vec.label_composite.value,
                                "s_hhi": round(vec.s_hhi, 4),
                                "s_icr": round(vec.s_icr, 4),
                                "s_macro": round(vec.s_macro, 4),
                                "dispatch_action": vec.dispatch.get("action", "HOLD_BASELINE"),
                            }
                            # Show adjustment delta if sensitivity was applied
                            if risk_tolerance > 0.0 and abs(tgt_pct - orig_tgt) > 1e-6:
                                order["sensitivity_adjustment"] = f"{orig_tgt:.2%} -> {tgt_pct:.2%}"

                        orders.append(order)
        return orders
