# battery_market_demo.py

from assume import World
from assume.agents import UnitOperatorAgent, MarketOperatorAgent
from assume.scenario import DirectScenarioLoader
import matplotlib.pyplot as plt
import csv

# =======================================================
# Tracking lists + CSV setup
# =======================================================
soc_history = []
trade_history = []

logfile = open("market_results.csv", "w", newline="")
csv_writer = csv.writer(logfile)
csv_writer.writerow(["round", "agent", "soc_mwh", "net_trade_mw", "price_eur_mwh"])


# =======================================================
# Bidding strategies
# =======================================================
class SimpleStorageStrategy:
    def __init__(self, max_bid_price=200.0, target_soc_fraction=0.6):
        self.max_bid_price = max_bid_price
        self.target_soc_frac = target_soc_fraction

    def calculate_bids(self, unit, market_config, product_tuples):
        bids = []
        for start, end in product_tuples:
            soc = unit.soc
            cap = unit.capacity_mwh
            target_soc = self.target_soc_frac * cap

            if soc > target_soc:
                bids.append({
                    "start_time": start,
                    "end_time": end,
                    "price": 0.7 * self.max_bid_price,
                    "volume": float(unit.max_discharge_mw),
                    "node": unit.name,
                })
            else:
                bids.append({
                    "start_time": start,
                    "end_time": end,
                    "price": 0.3 * self.max_bid_price,
                    "volume": float(-unit.max_charge_mw),
                    "node": unit.name,
                })
        return bids


class FixedDemandStrategy:
    def __init__(self, demand_mw=10.0, max_bid_price=300.0):
        self.demand_mw = demand_mw
        self.max_bid_price = max_bid_price

    def calculate_bids(self, unit, market_config, product_tuples):
        bids = []
        for start, end in product_tuples:
            bids.append({
                "start_time": start,
                "end_time": end,
                "price": self.max_bid_price,
                "volume": -self.demand_mw,
                "node": unit.name,
            })
        return bids


# =======================================================
# Market operator
# =======================================================
class ClearingMarketOperator(MarketOperatorAgent):
    def __init__(self, market_name, world, battery_unit=None, efficiency=0.92):
        super().__init__(market_name)
        self.world = world
        self.cleared_results = []
        self.battery_unit = battery_unit
        self.efficiency = efficiency

    def clear_market(self):
        all_orders = []
        for ob in self.received_orderbooks:
            all_orders.extend(ob["orderbook"])

        supply = sorted([o for o in all_orders if o["volume"] > 0],
                        key=lambda x: x["price"])
        demand = sorted([o for o in all_orders if o["volume"] < 0],
                        key=lambda x: -x["price"])

        supply_cursor, demand_cursor = 0, 0
        clearing_price = None
        matched_orders = []

        while supply_cursor < len(supply) and demand_cursor < len(demand):
            s = supply[supply_cursor]
            d = demand[demand_cursor]
            supply_vol = s["volume"]
            demand_vol = -d["volume"]

            if d["price"] >= s["price"]:
                traded = min(supply_vol, demand_vol)
                clearing_price = max(s["price"], d["price"])
                matched_orders.append({
                    "seller": s["node"],
                    "buyer": d["node"],
                    "volume": traded,
                    "price": clearing_price,
                })
                s["volume"] -= traded
                d["volume"] += traded
                if s["volume"] <= 0:
                    supply_cursor += 1
                if d["volume"] >= 0:
                    demand_cursor += 1
            else:
                break

        result = {
            "clearing_price": clearing_price,
            "matches": matched_orders,
        }
        self.cleared_results.append(result)
        print("\n=== Market cleared ===")
        print(result)

        # Update Battery SoC
        if self.battery_unit:
            delta_soc = 0.0
            for match in matched_orders:
                if match["seller"] == self.battery_unit.name:
                    delta_soc -= match["volume"]
                if match["buyer"] == self.battery_unit.name:
                    delta_soc += match["volume"] * self.efficiency
            self.battery_unit.soc = max(0.0, min(
                self.battery_unit.capacity_mwh,
                self.battery_unit.soc + delta_soc
            ))
            print(f"Updated Battery SoC = {self.battery_unit.soc:.2f} MWh")

        # Send results back
        per_agent_results = {}
        for match in matched_orders:
            per_agent_results.setdefault(match["seller"], []).append({
                "volume": -match["volume"], "price": match["price"],
            })
            per_agent_results.setdefault(match["buyer"], []).append({
                "volume": +match["volume"], "price": match["price"],
            })

        for agent_name, trades in per_agent_results.items():
            agent = self.world.get_agent(agent_name)
            if agent and hasattr(agent, "on_market_result"):
                agent.on_market_result(trades)

        return result


# =======================================================
# Unit operator with CSV logging
# =======================================================
class FeedbackUnitOperator(UnitOperatorAgent):
    def on_market_result(self, trades):
        net_volume = sum(t["volume"] for t in trades)
        avg_price = (sum(t["price"] * abs(t["volume"]) for t in trades) /
                     sum(abs(t["volume"]) for t in trades)) if trades else None

        trade_history.append(net_volume)
        soc_history.append(self.unit.soc)

        csv_writer.writerow([
            self.world.current_step,
            self.unit.name,
            self.unit.soc,
            net_volume,
            avg_price if avg_price else ""
        ])

        for t in trades:
            direction = "bought" if t["volume"] > 0 else "sold"
            print(f"[{self.unit.name}] {direction} {abs(t['volume'])} MW at {t['price']} €/MWh")


# =======================================================
# Build world, units, agents
# =======================================================
world = World()

battery_unit = world.register_unit(
    unit_type="StorageUnit", name="Battery1", node="N1",
    capacity_mwh=100.0, max_charge_mw=20.0, max_discharge_mw=20.0, soc=70.0
)
world.register_bidding_strategy("Battery1", SimpleStorageStrategy())

demand_unit = world.register_unit(
    unit_type="LoadUnit", name="Demand1", node="N1", capacity_mw=10.0
)
world.register_bidding_strategy("Demand1", FixedDemandStrategy(demand_mw=10.0))

world.add_agent(FeedbackUnitOperator(unit=battery_unit, strategy_name="Battery1"))
world.add_agent(FeedbackUnitOperator(unit=demand_unit, strategy_name="Demand1"))

market_operator = world.add_agent(ClearingMarketOperator(
    market_name="DayAhead", world=world, battery_unit=battery_unit, efficiency=0.92
))

scenario = {"markets": ["DayAhead"], "units": ["Battery1", "Demand1"]}
DirectScenarioLoader(world, scenario).load()

# ===========================================
