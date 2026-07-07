from typing import Dict, List, Any

class OrderDraftGenerator:
    def generate_order_draft(self, current_holdings: Dict[str, float], target_allocations: Dict[str, float], portfolio_value: float, etf_prices: Dict[str, float]) -> List[Dict[str, Any]]:
        orders = []
        all_etfs = set(current_holdings.keys()) | set(target_allocations.keys())
        for etf in sorted(list(all_etfs)):
            curr_pct = current_holdings.get(etf, 0.0)
            tgt_pct = target_allocations.get(etf, 0.0)
            delta = (tgt_pct - curr_pct) * portfolio_value
            if abs(delta) > 0.01:
                price = etf_prices.get(etf, 0.0)
                if price > 0:
                    shares = round(delta / price, 3)
                    if shares != 0:
                        orders.append({
                            "ETF": etf,
                            "Action": "BUY" if shares > 0 else "SELL",
                            "Shares": abs(shares),
                            "Target_Allocation_Pct": f"{tgt_pct:.2%}",
                            "Current_Allocation_Pct": f"{curr_pct:.2%}",
                            "Target_Value": f"${portfolio_value * tgt_pct:,.2f}",
                            "Current_Value": f"${portfolio_value * curr_pct:,.2f}",
                            "Delta_Value": f"${delta:,.2f}"
                        })
        return orders
