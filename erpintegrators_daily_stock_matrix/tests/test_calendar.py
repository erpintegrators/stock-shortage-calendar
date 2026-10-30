from datetime import datetime, timedelta
from unittest.mock import patch
from psycopg2.errors import SerializationFailure
import json
import logging
import time
from freezegun import freeze_time

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestStockShortageCalendar(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.warehouse = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.company.id)], limit=1
        )
        cls.other_company = cls.env["res.company"].create(
            {"name": "Calendar synthetic second company"}
        )
        cls.other_warehouse = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.other_company.id)], limit=1
        )
        cls.product = cls.env["product.product"].create(
            {"name": "Calendar synthetic item", "type": "consu", "is_storable": True}
        )
        cls.empty = cls.env["product.product"].create(
            {"name": "Calendar synthetic zero", "type": "consu", "is_storable": True}
        )
        cls.foreign_product = cls.env["product.product"].create(
            {
                "name": "Calendar foreign item",
                "type": "consu",
                "is_storable": True,
                "company_id": cls.other_company.id,
            }
        )
        cls.service = cls.env["product.product"].create(
            {"name": "Calendar synthetic service", "type": "service"}
        )
        cls.stock_user = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Calendar Inventory User",
                    "login": "calendar_inventory_fixture",
                    "company_id": cls.company.id,
                    "company_ids": [Command.set([cls.company.id, cls.other_company.id])],
                    "groups_id": [
                        Command.set(
                            [
                                cls.env.ref("base.group_user").id,
                                cls.env.ref("stock.group_stock_user").id,
                            ]
                        )
                    ],
                }
            )
        )
        cls.basic_user = (
            cls.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Calendar Basic User",
                    "login": "calendar_basic_fixture",
                    "company_id": cls.company.id,
                    "company_ids": [Command.set([cls.company.id])],
                    "groups_id": [Command.set([cls.env.ref("base.group_user").id])],
                }
            )
        )
        cls.api = (
            cls.env["stock.warehouse"]
            .with_user(cls.stock_user)
            .with_context(allowed_company_ids=[cls.company.id])
        )
        cls.env["stock.quant"]._update_available_quantity(
            cls.product, cls.warehouse.lot_stock_id, 10
        )
        cls.delivery = cls.make_move(cls.product, 12, 2, incoming=False)
        cls.receipt = cls.make_move(cls.product, 5, 4, incoming=True)

    @classmethod
    def make_move(cls, product, quantity, offset, incoming=False):
        source = (
            cls.env.ref("stock.stock_location_suppliers")
            if incoming
            else cls.warehouse.lot_stock_id
        )
        destination = (
            cls.warehouse.lot_stock_id
            if incoming
            else cls.env.ref("stock.stock_location_customers")
        )
        move = cls.env["stock.move"].create(
            {
                "name": "Calendar synthetic movement",
                "product_id": product.id,
                "product_uom": product.uom_id.id,
                "product_uom_qty": quantity,
                "location_id": source.id,
                "location_dest_id": destination.id,
                "company_id": cls.company.id,
            }
        )
        move._action_confirm()
        move.date = datetime.utcnow().replace(
            hour=12, minute=0, second=0, microsecond=0
        ) + timedelta(days=offset)
        return move

    def calendar(self, products=None, days=7):
        return self.api.erpintegrators_shortage_calendar(
            self.warehouse.id, products or [self.product.id], days
        )

    def test_01_arithmetic_and_first_shortage(self):
        result = self.calendar()
        self.assertEqual(
            [cell["quantity"] for cell in result["rows"][0]["cells"]], [10, 10, -2, -2, 3, 3, 3]
        )
        self.assertEqual(result["rows"][0]["first_shortage"], result["dates"][2])
        self.assertTrue(result["rows"][0]["cells"][2]["shortage"])

    def test_02_zero_stock(self):
        row = self.calendar([self.empty.id])["rows"][0]
        self.assertEqual([cell["quantity"] for cell in row["cells"]], [0] * 7)
        self.assertFalse(row["first_shortage"])

    def test_03_late_receipt(self):
        self.receipt.date += timedelta(days=2)
        self.assertEqual(
            [cell["quantity"] for cell in self.calendar()["rows"][0]["cells"]],
            [10, 10, -2, -2, -2, -2, 3],
        )

    def test_04_group_denied(self):
        api = self.api.with_user(self.basic_user)
        for method, arguments in [
            ("erpintegrators_calendar_choices", []),
            ("erpintegrators_shortage_calendar", [self.warehouse.id, [self.product.id], 7]),
            ("erpintegrators_calendar_forecast", [self.warehouse.id, self.product.id]),
        ]:
            with self.assertRaises(AccessError):
                getattr(api, method)(*arguments)

    def test_05_company_warehouse_denied(self):
        with self.assertRaises(AccessError):
            self.api.erpintegrators_shortage_calendar(self.other_warehouse.id, [self.product.id], 7)

    def test_06_company_product_denied(self):
        with self.assertRaises(AccessError):
            self.calendar([self.foreign_product.id])

    def test_07_product_input_bounds(self):
        for ids in [[], [True], [0], [-1], ["1"], [self.product.id] * 101, "1"]:
            with self.assertRaises(ValidationError):
                self.api.erpintegrators_shortage_calendar(self.warehouse.id, ids, 7)

    def test_08_warehouse_input_bounds(self):
        for value in [True, "1", 0, -1, None]:
            with self.assertRaises(ValidationError):
                self.api.erpintegrators_shortage_calendar(value, [self.product.id], 7)

    def test_09_horizon_input_bounds(self):
        for days in [True, "7", 0, 8, 32, 999]:
            with self.assertRaises(ValidationError):
                self.calendar(days=days)

    def test_10_date_horizons(self):
        for days in [7, 14, 31]:
            result = self.calendar(days=days)
            self.assertEqual(len(result["dates"]), days)
            self.assertEqual(result["dates"][0], datetime.utcnow().date().isoformat())
            self.assertEqual(
                result["dates"][-1],
                (datetime.utcnow().date() + timedelta(days=days - 1)).isoformat(),
            )
            self.assertEqual(result["timezone"], "UTC")

    def test_11_choices_filter(self):
        result = self.api.erpintegrators_calendar_choices("Calendar synthetic")
        ids = [row["id"] for row in result["products"]]
        self.assertIn(self.product.id, ids)
        self.assertIn(self.empty.id, ids)
        self.assertNotIn(self.service.id, ids)
        self.assertNotIn(self.foreign_product.id, ids)
        self.assertNotIn(self.other_warehouse.id, [row["id"] for row in result["warehouses"]])

    def test_12_forecast_drilldown(self):
        result = self.api.erpintegrators_calendar_forecast(self.warehouse.id, self.product.id)
        self.assertEqual(result["tag"], "stock_forecasted")
        self.assertEqual(result["context"]["active_id"], self.product.id)
        self.assertEqual(result["context"]["warehouse_id"], self.warehouse.id)

    def test_13_read_only(self):
        before = self.env["stock.move"].search([]).read(["state", "product_uom_qty", "date"])
        quants = self.env["stock.quant"].search([]).read(["quantity", "reserved_quantity"])
        self.calendar()
        self.api.erpintegrators_calendar_choices()
        self.api.erpintegrators_calendar_forecast(self.warehouse.id, self.product.id)
        self.assertEqual(
            before, self.env["stock.move"].search([]).read(["state", "product_uom_qty", "date"])
        )
        self.assertEqual(
            quants, self.env["stock.quant"].search([]).read(["quantity", "reserved_quantity"])
        )

    def test_14_search_bounds(self):
        for term in [None, True, "x" * 121]:
            with self.assertRaises(ValidationError):
                self.api.erpintegrators_calendar_choices(term)

    def test_15_config_guard(self):
        self.env["ir.config_parameter"].set_param("stock.report_stock_quantity_period", "1")
        with self.assertRaises(UserError):
            self.calendar()

    def test_16_native_failures_propagate(self):
        with patch.object(
            type(self.env["report.stock.quantity"]),
            "read_group",
            side_effect=AccessError("Fixture report denial"),
        ):
            with self.assertRaises(AccessError):
                self.calendar()

    def test_17_inactive_service_and_missing_denied(self):
        for product_id in [self.service.id, max(self.env["product.product"].search([]).ids) + 100]:
            with self.assertRaises(AccessError):
                self.calendar([product_id])
        self.product.active = False
        with self.assertRaises(AccessError):
            self.calendar()

    def test_18_timezone_invariant(self):
        expected = self.calendar()
        for timezone in ["America/Edmonton", "America/New_York", "Pacific/Auckland"]:
            actual = self.api.with_context(tz=timezone).erpintegrators_shortage_calendar(
                self.warehouse.id, [self.product.id], 7
            )
            self.assertEqual(actual, expected)

    def test_19_product_order(self):
        result = self.calendar([self.empty.id, self.product.id])
        self.assertEqual([row["id"] for row in result["rows"]], [self.empty.id, self.product.id])

    def test_20_native_parity(self):
        result = self.calendar()
        for date, cell in zip(result["dates"], result["rows"][0]["cells"]):
            self.env.cr.execute(
                """SELECT COALESCE(SUM(product_qty), 0) FROM report_stock_quantity
                WHERE company_id = %s AND warehouse_id = %s AND product_id = %s
                AND date = %s AND state = 'forecast'""",
                [self.company.id, self.warehouse.id, self.product.id, date],
            )
            self.assertEqual(cell["quantity"], self.env.cr.fetchone()[0])

    def test_21_duplicate_selection_normalizes(self):
        result = self.calendar([self.product.id, self.product.id, self.empty.id])
        self.assertEqual([row["id"] for row in result["rows"]], [self.product.id, self.empty.id])

    def test_22_maximum_positive_shape(self):
        products = self.env["product.product"].create(
            [
                {"name": "Calendar capacity item %s" % number, "type": "consu", "is_storable": True}
                for number in range(100)
            ]
        )
        result = self.calendar(products.ids, days=31)
        self.assertEqual(len(result["rows"]), 100)
        self.assertTrue(all(len(row["cells"]) == 31 for row in result["rows"]))

    def test_23_portal_denied(self):
        user = (
            self.env["res.users"]
            .with_context(no_reset_password=True)
            .create(
                {
                    "name": "Calendar Portal Fixture",
                    "login": "calendar_portal_fixture",
                    "groups_id": [Command.set([self.env.ref("base.group_portal").id])],
                }
            )
        )
        api = self.api.with_user(user)
        for method, arguments in [
            ("erpintegrators_calendar_choices", []),
            ("erpintegrators_shortage_calendar", [self.warehouse.id, [self.product.id], 7]),
            ("erpintegrators_calendar_forecast", [self.warehouse.id, self.product.id]),
        ]:
            with self.assertRaises(AccessError):
                getattr(api, method)(*arguments)

    def test_24_uom_precision(self):
        unit = self.env.ref("uom.product_uom_kgm")
        product = self.env["product.product"].create(
            {
                "name": "Calendar precision item",
                "type": "consu",
                "is_storable": True,
                "uom_id": unit.id,
                "uom_po_id": unit.id,
            }
        )
        self.env["stock.quant"]._update_available_quantity(
            product, self.warehouse.lot_stock_id, -unit.rounding / 10
        )
        row = self.calendar([product.id])["rows"][0]
        self.assertFalse(row["first_shortage"])
        self.assertTrue(all(cell["quantity"] == 0 for cell in row["cells"]))

    def test_25_utc_midnight(self):
        start = datetime.utcnow().replace(hour=23, minute=59, second=59)
        with freeze_time(start) as clock:
            before = self.calendar()
            clock.tick(timedelta(seconds=2))
            after = self.api.with_context(
                tz="America/Los_Angeles"
            ).erpintegrators_shortage_calendar(self.warehouse.id, [self.product.id], 7)
        self.assertEqual(before["dates"][0], start.date().isoformat())
        self.assertEqual(after["dates"][0], (start.date() + timedelta(days=1)).isoformat())

    def test_26_rebuilt_custom_window_denied(self):
        config = self.env["ir.config_parameter"]
        config.set_param("stock.report_stock_quantity_period", "1")
        self.env["report.stock.quantity"].init()
        config.set_param("stock.report_stock_quantity_period", "3")
        with self.assertRaises(UserError):
            self.calendar()
        self.env["report.stock.quantity"].init()
        self.assertEqual(len(self.calendar()["dates"]), 7)

    def test_27_earlier_receipt(self):
        self.receipt.date -= timedelta(days=3)
        self.assertFalse(self.calendar()["rows"][0]["first_shortage"])
        self.assertEqual(
            [c["quantity"] for c in self.calendar()["rows"][0]["cells"]], [10, 15, 3, 3, 3, 3, 3]
        )

    def test_28_internal_move(self):
        shelf = self.env["stock.location"].create(
            {
                "name": "Calendar shelf",
                "usage": "internal",
                "location_id": self.warehouse.lot_stock_id.id,
            }
        )
        before = self.calendar()
        move = self.make_move(self.product, 4, 1)
        move.location_dest_id = shelf
        self.assertEqual(self.calendar(), before)

    def test_29_interwarehouse_transfer(self):
        other = self.env["stock.warehouse"].create(
            {"name": "Calendar second warehouse", "code": "CSW", "company_id": self.company.id}
        )
        move = self.make_move(self.product, 4, 1)
        move.location_dest_id = other.lot_stock_id
        source = self.calendar()["rows"][0]["cells"]
        destination = self.api.erpintegrators_shortage_calendar(other.id, [self.product.id], 7)[
            "rows"
        ][0]["cells"]
        self.assertEqual([c["quantity"] for c in source], [10, 6, -6, -6, -1, -1, -1])
        self.assertEqual([c["quantity"] for c in destination], [0, 4, 4, 4, 4, 4, 4])
        action = self.api.erpintegrators_calendar_forecast(other.id, self.product.id)
        self.assertEqual(action["context"]["warehouse_id"], other.id)

    def test_30_done_return_and_cancel(self):
        move = self.make_move(self.product, 2, 0, incoming=True)
        move.quantity = 2
        move.picked = True
        move._action_done()
        self.assertEqual(self.calendar()["rows"][0]["cells"][0]["quantity"], 12)
        cancelled = self.make_move(self.product, 999, 1)
        cancelled._action_cancel()
        self.assertEqual(self.calendar()["rows"][0]["cells"][1]["quantity"], 12)

    def test_31_record_rule(self):
        self.env["ir.rule"].create(
            {
                "name": "Calendar restrictive fixture",
                "model_id": self.env["ir.model"]._get_id("product.product"),
                "domain_force": "[('id', '!=', %s)]" % self.product.id,
            }
        )
        with self.assertRaises(AccessError):
            self.calendar()
        self.assertNotIn(
            self.product.id,
            [p["id"] for p in self.api.erpintegrators_calendar_choices()["products"]],
        )
        with self.assertRaises(AccessError):
            self.api.erpintegrators_calendar_forecast(self.warehouse.id, self.product.id)

    def test_32_company_switch(self):
        api = self.api.with_context(allowed_company_ids=[self.other_company.id])
        self.assertEqual(api.erpintegrators_calendar_choices()["company"], self.other_company.name)
        result = api.erpintegrators_shortage_calendar(
            self.other_warehouse.id, [self.foreign_product.id], 7
        )
        self.assertEqual(result["company"], self.other_company.name)
        with self.assertRaises(AccessError):
            api.erpintegrators_shortage_calendar(self.warehouse.id, [self.product.id], 7)

    def test_33_precision_negative(self):
        unit = self.env.ref("uom.product_uom_kgm")
        product = self.env["product.product"].create(
            {
                "name": "Calendar fraction",
                "type": "consu",
                "is_storable": True,
                "uom_id": unit.id,
                "uom_po_id": unit.id,
            }
        )
        self.env["stock.quant"]._update_available_quantity(
            product, self.warehouse.lot_stock_id, -1.25
        )
        row = self.calendar([product.id])["rows"][0]
        self.assertEqual(row["cells"][0]["quantity"], -1.25)
        self.assertTrue(row["first_shortage"])

    def test_34_repeated_reads_snapshot(self):
        models = {
            "stock.move": ["state", "quantity", "date"],
            "stock.quant": ["quantity", "reserved_quantity"],
            "stock.picking": ["state", "scheduled_date"],
            "product.product": ["name", "active", "write_date"],
        }
        before = {name: self.env[name].search([]).read(fields) for name, fields in models.items()}
        for unused in range(3):
            self.calendar()
            self.api.erpintegrators_calendar_forecast(self.warehouse.id, self.product.id)
            with self.assertRaises(AccessError):
                self.calendar([self.foreign_product.id])
        after = {name: self.env[name].search([]).read(fields) for name, fields in models.items()}
        self.assertEqual(before, after)

    def test_35_scale_5000_moves(self):
        products = self.env["product.product"].create(
            [
                {"name": "Calendar benchmark %03d" % i, "type": "consu", "is_storable": True}
                for i in range(100)
            ]
        )
        values = []
        for i in range(5000):
            product = products[i % 100]
            values.append(
                {
                    "name": "Calendar benchmark move",
                    "product_id": product.id,
                    "product_uom": product.uom_id.id,
                    "product_uom_qty": 1,
                    "company_id": self.company.id,
                    "state": "confirmed",
                    "location_id": self.env.ref("stock.stock_location_suppliers").id,
                    "location_dest_id": self.warehouse.lot_stock_id.id,
                    "date": datetime.utcnow() + timedelta(days=i % 31),
                }
            )
        self.env["stock.move"].create(values)
        self.env.flush_all()
        self.calendar(products.ids, 31)
        elapsed = []
        report_type = type(self.env["report.stock.quantity"])
        original = report_type.read_group
        for unused in range(3):
            with patch.object(
                report_type, "read_group", autospec=True, side_effect=original
            ) as aggregation:
                begin = time.perf_counter()
                result = self.calendar(products.ids, 31)
                elapsed.append(time.perf_counter() - begin)
                self.assertEqual(aggregation.call_count, 1)
            self.assertEqual(len(result["rows"]), 100)
            self.assertTrue(all(len(row["cells"]) == 31 for row in result["rows"]))
        logging.getLogger(__name__).info(
            "CALENDAR_BENCHMARK %s",
            json.dumps({"moves": 5000, "products": 100, "days": 31, "warm_seconds": elapsed}),
        )
        self.assertLess(max(elapsed), 2.0)

    def test_36_untracked_consumable_denied(self):
        product = self.env["product.product"].create(
            {"name": "Calendar untracked consumable", "type": "consu", "is_storable": False}
        )
        with self.assertRaises(AccessError):
            self.calendar([product.id])
        choices = self.api.erpintegrators_calendar_choices("Calendar untracked consumable")
        self.assertEqual(choices["products"], [])

    def test_37_final_destination_forecast(self):
        other = self.env["stock.warehouse"].create(
            {"name": "Calendar final destination", "code": "CFD", "company_id": self.company.id}
        )
        move = self.make_move(self.product, 4, 1)
        move.write({"location_final_id": other.lot_stock_id.id})
        destination = self.api.erpintegrators_shortage_calendar(other.id, [self.product.id], 7)
        self.assertEqual(
            [cell["quantity"] for cell in destination["rows"][0]["cells"]], [0, 4, 4, 4, 4, 4, 4]
        )
        action = self.api.erpintegrators_calendar_forecast(other.id, self.product.id)
        self.assertEqual(action["context"]["warehouse_id"], other.id)

    def explain(self, offset=2, days=7):
        return self.api.erpintegrators_explain_shortage(
            self.warehouse.id,
            self.product.id,
            (datetime.utcnow().date() + timedelta(days=offset)).isoformat(),
            days,
        )

    def test_40_worklist_metrics(self):
        row = self.calendar()["rows"][0]
        self.assertEqual(row["worst_deficit"], 2)
        self.assertEqual(row["shortage_days"], 2)
        self.assertEqual(row["first_recovery"], self.calendar()["dates"][4])
        self.assertFalse(row["recurs"])
        self.assertEqual(self.calendar([self.empty.id])["rows"][0]["shortage_days"], 0)

    def test_41_recurrence_and_no_recovery(self):
        self.make_move(self.product, 7, 5)
        row = self.calendar()["rows"][0]
        self.assertEqual(row["worst_deficit"], 4)
        self.assertEqual(row["shortage_days"], 4)
        self.assertTrue(row["recurs"])
        self.receipt._action_cancel()
        row = self.calendar()["rows"][0]
        self.assertFalse(row["first_recovery"])
        self.assertFalse(row["recurs"])

    def test_42_watchlist_save_restore_and_owner(self):
        self.assertFalse(self.api.erpintegrators_load_watchlist()["exists"])
        self.api.erpintegrators_save_watchlist(self.warehouse.id, [self.empty.id, self.product.id])
        self.assertEqual(
            {p["id"] for p in self.api.erpintegrators_load_watchlist()["products"]},
            {self.product.id, self.empty.id},
        )
        self.assertFalse(
            self.api.with_user(self.env.user).erpintegrators_load_watchlist()["exists"]
        )
        self.api.erpintegrators_save_watchlist(self.warehouse.id, [self.product.id])
        self.assertEqual(len(self.api.erpintegrators_load_watchlist()["products"]), 1)

    def test_43_watchlist_company_and_unavailable(self):
        self.api.erpintegrators_save_watchlist(self.warehouse.id, [self.product.id, self.empty.id])
        other = self.api.with_context(allowed_company_ids=[self.other_company.id])
        self.assertFalse(other.erpintegrators_load_watchlist()["exists"])
        self.empty.active = False
        result = self.api.erpintegrators_load_watchlist()
        self.assertEqual(result["unavailable"], 1)
        self.assertEqual([p["id"] for p in result["products"]], [self.product.id])

    def test_44_watchlist_record_rules_and_forgery(self):
        model = self.env["erpintegrators.stock.watchlist"].with_user(self.stock_user)
        with self.assertRaises(AccessError):
            model.create({"user_id": self.basic_user.id, "product_ids": [self.product.id]})
        with self.assertRaises(AccessError):
            model.create({"company_id": self.other_company.id, "product_ids": [self.product.id]})
        self.api.erpintegrators_save_watchlist(self.warehouse.id, [self.product.id])
        saved = model.search([])
        with self.assertRaises(AccessError):
            saved.write({"user_id": self.basic_user.id})
        for ids in [[], [True], [self.product.id] * 101, "invalid"]:
            with self.assertRaises(ValidationError):
                saved.write({"product_ids": ids})
        self.env["ir.rule"].create(
            {
                "name": "Watchlist product restriction",
                "model_id": self.env["ir.model"]._get_id("product.product"),
                "domain_force": "[('id', '!=', %s)]" % self.product.id,
            }
        )
        self.assertEqual(self.api.erpintegrators_load_watchlist()["products"], [])
        self.assertEqual(self.api.erpintegrators_load_watchlist()["unavailable"], 1)

    def test_45_explanation_balance(self):
        result = self.explain()
        self.assertEqual(
            [result[k] for k in ["opening", "incoming", "outgoing", "closing"]], [10, 0, 12, -2]
        )
        self.assertEqual(
            {r["id"] for r in result["movements"]}, {self.delivery.id, self.receipt.id}
        )
        for offset in range(7):
            result = self.explain(offset)
            self.assertAlmostEqual(
                result["opening"] + result["incoming"] - result["outgoing"], result["closing"]
            )
        self.assertEqual(self.explain(4)["incoming"], 5)

    def test_46_explanation_boundaries(self):
        today = datetime.utcnow().date()
        for date in [
            None,
            True,
            "invalid",
            today.isoformat() + "T00:00:00",
            (today - timedelta(days=1)).isoformat(),
            (today + timedelta(days=7)).isoformat(),
        ]:
            with self.assertRaises(ValidationError):
                self.api.erpintegrators_explain_shortage(
                    self.warehouse.id, self.product.id, date, 7
                )
        for method, arguments in [
            (
                "erpintegrators_explain_shortage",
                [self.warehouse.id, self.product.id, today.isoformat(), 7],
            ),
            ("erpintegrators_save_watchlist", [self.warehouse.id, [self.product.id]]),
            ("erpintegrators_load_watchlist", []),
            (
                "erpintegrators_open_movement",
                [self.warehouse.id, self.product.id, self.delivery.id, 7],
            ),
        ]:
            with self.assertRaises(AccessError):
                getattr(self.api.with_user(self.basic_user), method)(*arguments)
        with self.assertRaises(AccessError):
            self.api.erpintegrators_explain_shortage(
                self.other_warehouse.id, self.product.id, today.isoformat()
            )

    def test_47_sources_cancel_internal_and_transfer(self):
        cancelled = self.make_move(self.product, 99, 1)
        cancelled._action_cancel()
        shelf = self.env["stock.location"].create(
            {
                "name": "Explanation shelf",
                "usage": "internal",
                "location_id": self.warehouse.lot_stock_id.id,
            }
        )
        internal = self.make_move(self.product, 3, 1)
        internal.location_dest_id = shelf
        other = self.env["stock.warehouse"].create(
            {"name": "Explanation warehouse", "code": "EXW", "company_id": self.company.id}
        )
        transfer = self.make_move(self.product, 4, 1)
        transfer.location_dest_id = other.lot_stock_id
        result = self.explain(1)
        ids = {m["id"] for m in result["movements"]}
        self.assertNotIn(cancelled.id, ids)
        self.assertNotIn(internal.id, ids)
        self.assertIn(transfer.id, ids)
        self.assertEqual(result["outgoing"], 4)

    def test_48_sources_partial_and_done(self):
        self.receipt.quantity = 2
        self.receipt.picked = True
        self.receipt._action_done()
        remainder = self.env["stock.move"].search(
            [
                ("product_id", "=", self.product.id),
                ("state", "not in", ["done", "cancel"]),
                ("location_id", "=", self.env.ref("stock.stock_location_suppliers").id),
            ]
        )
        self.assertEqual(len(remainder), 1)
        remainder.date = datetime.utcnow().replace(
            hour=12, minute=0, second=0, microsecond=0
        ) + timedelta(days=4)
        result = self.explain(4)
        self.assertEqual(result["incoming"], 3)
        self.assertEqual(result["closing"], 3)
        self.assertEqual(
            next(m["quantity"] for m in result["movements"] if m["id"] == remainder.id), 3
        )
        move = self.make_move(self.product, 2, 0, incoming=True)
        move.quantity = 2
        move.picked = True
        move._action_done()
        self.assertNotIn(move.id, {m["id"] for m in self.explain()["movements"]})

    def test_49_overdue_and_source_limits(self):
        self.delivery.date = datetime.utcnow() - timedelta(days=1)
        result = self.explain(0)
        self.assertEqual(result["closing"], -2)
        self.assertEqual(result["outgoing"], 0)
        self.assertTrue(
            next(m["overdue"] for m in result["movements"] if m["id"] == self.delivery.id)
        )
        values = []
        for number in range(102):
            values.append(
                {
                    "name": "Explanation bounded move %s" % number,
                    "product_id": self.product.id,
                    "product_uom": self.product.uom_id.id,
                    "product_uom_qty": 1,
                    "company_id": self.company.id,
                    "location_id": self.env.ref("stock.stock_location_suppliers").id,
                    "location_dest_id": self.warehouse.lot_stock_id.id,
                    "state": "confirmed",
                    "date": datetime.utcnow() + timedelta(days=1),
                }
            )
        self.env["stock.move"].create(values)
        result = self.explain()
        self.assertEqual(len(result["movements"]), 100)
        self.assertTrue(result["truncated"])

    def test_50_source_access_and_recheck(self):
        action = self.api.erpintegrators_open_movement(
            self.warehouse.id, self.product.id, self.delivery.id, 7
        )
        self.assertEqual(action["res_id"], self.delivery.id)
        self.assertEqual(action["res_model"], "stock.move")
        self.env["ir.rule"].create(
            {
                "name": "Explanation hidden movement",
                "model_id": self.env["ir.model"]._get_id("stock.move"),
                "domain_force": "[('id', '!=', %s)]" % self.delivery.id,
            }
        )
        self.assertNotIn(self.delivery.id, {m["id"] for m in self.explain()["movements"]})
        with self.assertRaises(AccessError):
            self.api.erpintegrators_open_movement(
                self.warehouse.id, self.product.id, self.delivery.id, 7
            )
        self.env["ir.rule"].create(
            {
                "name": "Explanation hidden destination",
                "model_id": self.env["ir.model"]._get_id("stock.location"),
                "domain_force": "[('id', '!=', %s)]" % self.warehouse.lot_stock_id.id,
            }
        )
        self.assertEqual(self.explain()["movements"], [])

    def test_51_explanation_read_only_and_reschedule(self):
        before = self.env["stock.move"].search([]).read(["state", "date", "product_uom_qty"])
        quants = self.env["stock.quant"].search([]).read(["quantity", "reserved_quantity"])
        self.explain()
        self.api.erpintegrators_save_watchlist(self.warehouse.id, [self.product.id])
        self.api.erpintegrators_load_watchlist()
        self.assertEqual(
            before, self.env["stock.move"].search([]).read(["state", "date", "product_uom_qty"])
        )
        self.assertEqual(
            quants, self.env["stock.quant"].search([]).read(["quantity", "reserved_quantity"])
        )
        self.receipt.date += timedelta(days=2)
        row = self.calendar()["rows"][0]
        self.assertEqual(row["shortage_days"], 4)
        self.assertEqual(row["first_recovery"], self.calendar()["dates"][6])

    def test_52_source_document_permissions(self):
        picking = self.env["stock.picking"].create(
            {
                "picking_type_id": self.warehouse.in_type_id.id,
                "location_id": self.receipt.location_id.id,
                "location_dest_id": self.receipt.location_dest_id.id,
            }
        )
        self.receipt.picking_id = picking
        action = self.api.erpintegrators_open_movement(
            self.warehouse.id, self.product.id, self.receipt.id, 7
        )
        self.assertEqual(action["res_model"], "stock.picking")
        self.assertEqual(action["res_id"], picking.id)
        self.env["ir.rule"].create(
            {
                "name": "Explanation hidden receipt",
                "model_id": self.env["ir.model"]._get_id("stock.picking"),
                "domain_force": "[('id', '!=', %s)]" % picking.id,
            }
        )
        with self.assertRaises(AccessError):
            self.api.erpintegrators_open_movement(
                self.warehouse.id, self.product.id, self.receipt.id, 7
            )
        self.assertNotIn(picking.name, {m["name"] for m in self.explain()["movements"]})

    def test_54_final_destination_sources(self):
        other = self.env["stock.warehouse"].create(
            {"name": "Explanation final destination", "code": "EFD", "company_id": self.company.id}
        )
        self.receipt.location_final_id = other.lot_stock_id
        self.assertNotIn(self.receipt.id, {m["id"] for m in self.explain(4)["movements"]})
        result = self.api.erpintegrators_explain_shortage(
            other.id, self.product.id, (datetime.utcnow().date() + timedelta(days=4)).isoformat(), 7
        )
        self.assertEqual(result["incoming"], 5)
        self.assertIn(self.receipt.id, {m["id"] for m in result["movements"]})

    def workflow_row(self):
        return self.calendar()["rows"][0]["workflow"]

    def save_followup(self, revision=0, state="open", token=None, note="Call supplier"):
        return self.api.erpintegrators_save_shortage_action(
            self.warehouse.id,
            self.product.id,
            7,
            token or self.workflow_row()["token"],
            revision,
            self.stock_user.id,
            datetime.utcnow().date().isoformat(),
            state,
            note,
        )

    def receipt_choice(self, move=None):
        ident = (move or self.receipt).id
        return next(
            r
            for r in self.api.erpintegrators_simulation_choices(
                self.warehouse.id, self.product.id, 7
            )["receipts"]
            if r["id"] == ident
        )

    def preview_receipt(self, offset, move=None, token=None):
        move = move or self.receipt
        return self.api.erpintegrators_simulate_receipt(
            self.warehouse.id,
            self.product.id,
            move.id,
            (datetime.utcnow().date() + timedelta(days=offset)).isoformat(),
            token or self.receipt_choice(move)["token"],
            7,
        )

    def test_60_workflow_baseline_atomic(self):
        self.api.erpintegrators_capture_baseline(self.warehouse.id, [self.product.id], 7)
        model = self.env["erpintegrators.shortage.snapshot"]
        old = model.search([]).read(["basis", "captured_at"])
        self.receipt.date += timedelta(days=1)
        with self.assertRaises(UserError):
            with patch.object(
                type(model),
                "_create_internal",
                side_effect=UserError("Controlled baseline failure"),
            ):
                self.api.erpintegrators_capture_baseline(
                    self.warehouse.id, [self.product.id, self.empty.id], 7
                )
        self.assertEqual(model.search([]).read(["basis", "captured_at"]), old)

    def test_61_workflow_change_labels(self):
        self.api.erpintegrators_capture_baseline(self.warehouse.id, [self.product.id], 7)
        self.assertEqual(self.workflow_row()["comparison"]["status"], "unchanged")
        self.delivery.product_uom_qty = 14
        self.assertEqual(self.workflow_row()["comparison"]["status"], "worsening")
        self.delivery.product_uom_qty = 11
        self.assertEqual(self.workflow_row()["comparison"]["status"], "improved")
        self.delivery.product_uom_qty = 8
        self.assertEqual(self.workflow_row()["comparison"]["status"], "cleared")
        self.api.erpintegrators_capture_baseline(self.warehouse.id, [self.product.id], 7)
        self.delivery.product_uom_qty = 12
        self.assertEqual(self.workflow_row()["comparison"]["status"], "new_shortage")

    def test_62_workflow_comparable_dates(self):
        from ..models.shortage_workflow import compare_balances

        a = {
            "unit": 1,
            "calculation": "x",
            "cells": [
                {"date": "2026-01-01", "quantity": -2},
                {"date": "2026-01-02", "quantity": 0},
            ],
        }
        b = {
            "unit": 1,
            "calculation": "x",
            "cells": [{"date": "2026-01-02", "quantity": 0}, {"date": "2026-01-03", "quantity": 0}],
        }
        self.assertEqual(compare_balances(a, b)["status"], "unchanged")
        b["unit"] = 2
        self.assertEqual(compare_balances(a, b)["status"], "incompatible")
        b["unit"] = 1
        b["cells"] = [{"date": "2026-01-03", "quantity": 0}]
        self.assertEqual(compare_balances(a, b)["status"], "no_overlap")
        self.api.erpintegrators_capture_baseline(self.warehouse.id, [self.product.id], 7)
        row = self.calendar([self.empty.id])["rows"][0]
        self.assertEqual(row["workflow"]["comparison"]["status"], "no_baseline")

    def test_81_workflow_extended_shortage(self):
        self.api.erpintegrators_capture_baseline(self.warehouse.id, [self.product.id], 7)
        self.receipt.date += timedelta(days=2)
        self.assertEqual(self.workflow_row()["comparison"]["status"], "worsening")

    def test_63_workflow_sources(self):
        self.api.erpintegrators_capture_baseline(self.warehouse.id, [self.product.id], 7)
        self.receipt.date += timedelta(days=1)
        self.receipt.product_uom_qty = 7
        changes = self.workflow_row()["comparison"]["source_changes"]
        row = next(c for c in changes if c["move_id"] == self.receipt.id)
        self.assertEqual((row["old_quantity"], row["quantity"]), (5, 7))
        self.assertNotEqual(row["old_date"], row["date"])
        self.receipt._action_cancel()
        self.assertEqual(
            next(
                c
                for c in self.workflow_row()["comparison"]["source_changes"]
                if c["move_id"] == self.receipt.id
            )["kind"],
            "no_longer_open",
        )

    def test_64_workflow_retention(self):
        self.api.erpintegrators_capture_baseline(self.warehouse.id, [self.product.id], 7)
        model = self.env["erpintegrators.shortage.snapshot"]
        row = model.search([])
        row._write_internal({"captured_at": fields.Datetime.now() - timedelta(days=91)})
        self.assertEqual(self.workflow_row()["comparison"]["status"], "no_baseline")
        with self.assertRaises(AccessError):
            model.with_user(self.stock_user)._cron_prune()
        model._cron_prune()
        self.assertFalse(model.search([]))

    def test_65_workflow_guarded_storage(self):
        self.api.erpintegrators_capture_baseline(self.warehouse.id, [self.product.id], 7)
        self.save_followup()
        for name in ["snapshot", "source", "action", "history"]:
            model = self.env["erpintegrators.shortage." + name].with_user(self.stock_user)
            with self.assertRaises(AccessError):
                model.create({})
            existing = model.search([])
            self.assertTrue(existing)
            with self.assertRaises(AccessError):
                existing.write({})
            with self.assertRaises(AccessError):
                existing.unlink()

    def test_66_workflow_product_source_rules(self):
        self.api.erpintegrators_capture_baseline(self.warehouse.id, [self.product.id], 7)
        self.save_followup()
        snapshots = self.env["erpintegrators.shortage.snapshot"].with_user(self.stock_user)
        sources = self.env["erpintegrators.shortage.source"].with_user(self.stock_user)
        hidden = sources.search([("move_id", "=", self.receipt.id)])
        source_rule = self.env["ir.rule"].create(
            {
                "name": "Workflow source restriction",
                "model_id": self.env["ir.model"]._get_id("stock.move"),
                "domain_force": "[('id','!=',%s)]" % self.receipt.id,
            }
        )
        self.assertNotIn(self.receipt.id, sources.search([]).mapped("move_id").ids)
        with self.assertRaises(AccessError):
            hidden.read(["facts"])
        self.api.erpintegrators_capture_baseline(self.warehouse.id, [self.product.id], 7)
        source_rule.unlink()
        self.assertFalse(
            sources.search(
                [("snapshot_id", "=", snapshots.search([]).id), ("move_id", "=", self.receipt.id)]
            )
        )
        self.env["ir.rule"].create(
            {
                "name": "Workflow product restriction",
                "model_id": self.env["ir.model"]._get_id("product.product"),
                "domain_force": "[('id','!=',%s)]" % self.product.id,
            }
        )
        self.assertFalse(snapshots.search([]))
        self.assertFalse(
            self.env["erpintegrators.shortage.action"].with_user(self.stock_user).search([])
        )

    def test_67_workflow_owner_validation(self):
        token = self.workflow_row()["token"]
        today = datetime.utcnow().date().isoformat()
        for owner, due, state, note in [
            (True, today, "open", "x"),
            (self.basic_user.id, today, "open", "x"),
            (self.stock_user.id, "invalid", "open", "x"),
            (self.stock_user.id, today, "invalid", "x"),
            (self.stock_user.id, today, "open", "x" * 2001),
        ]:
            with self.assertRaises(ValidationError):
                self.api.erpintegrators_save_shortage_action(
                    self.warehouse.id, self.product.id, 7, token, 0, owner, due, state, note
                )

    def test_68_workflow_revision_conflict(self):
        self.assertEqual(self.save_followup()["revision"], 1)
        self.assertEqual(self.save_followup(1, note="Updated")["revision"], 2)
        with self.assertRaises(ValidationError):
            self.save_followup(1, note="Stale edit")
        self.assertEqual(self.workflow_row()["action"]["note"], "Updated")

    def test_69_workflow_done_and_review(self):
        self.save_followup(state="done")
        row = self.workflow_row()
        self.assertTrue(row["forecast_short"])
        self.assertEqual(row["action"]["state"], "done")
        self.receipt.date += timedelta(days=2)
        self.assertTrue(self.workflow_row()["action"]["needs_review"])
        self.save_followup(1, state="done")
        self.assertFalse(self.workflow_row()["action"]["needs_review"])

    def test_70_workflow_history(self):
        self.save_followup()
        for revision in range(1, 52):
            self.save_followup(revision, note="Revision %s" % revision)
        model = self.env["erpintegrators.shortage.history"]
        rows = model.search([])
        self.assertEqual(len(rows), 50)
        self.assertEqual(rows[0].revision, 52)
        self.assertEqual(rows[0].actor_id, self.stock_user)
        rows[:1]._write_internal({"changed_at": fields.Datetime.now() - timedelta(days=366)})
        self.env["erpintegrators.shortage.snapshot"]._cron_prune()
        self.assertEqual(len(model.search([])), 49)

    def test_71_workflow_simulation_native_parity(self):
        for offset in [1, 6]:
            result = self.preview_receipt(offset)
            original = self.receipt.date
            self.receipt.date = datetime.utcnow().replace(
                hour=12, minute=0, second=0, microsecond=0
            ) + timedelta(days=offset)
            self.assertEqual(
                [c["quantity"] for c in result["cells"]],
                [c["quantity"] for c in self.calendar()["rows"][0]["cells"]],
            )
            self.receipt.date = original

    def test_72_workflow_partial_cancel_overdue(self):
        self.receipt.quantity = 2
        self.receipt.picked = True
        self.receipt._action_done()
        remainder = self.env["stock.move"].search(
            [
                ("product_id", "=", self.product.id),
                ("state", "not in", ["done", "cancel"]),
                ("location_id.usage", "=", "supplier"),
            ]
        )
        remainder.date = datetime.utcnow() + timedelta(days=4)
        self.assertEqual(self.preview_receipt(6, remainder)["quantity"], 3)
        remainder.date = datetime.utcnow() - timedelta(days=1)
        self.assertEqual(self.preview_receipt(4, remainder)["cells"][0]["delta"], -3)
        token = self.receipt_choice(remainder)["token"]
        remainder._action_cancel()
        with self.assertRaises(AccessError):
            self.preview_receipt(4, remainder, token)

    def test_73_workflow_multistep(self):
        location = self.env["stock.location"].create(
            {
                "name": "Workflow input",
                "usage": "internal",
                "location_id": self.warehouse.view_location_id.id,
            }
        )
        self.receipt.location_dest_id = location
        leg = self.make_move(self.product, 5, 4, incoming=True)
        leg.location_id = location
        leg.location_dest_id = self.warehouse.lot_stock_id
        choices = self.api.erpintegrators_simulation_choices(self.warehouse.id, self.product.id, 7)[
            "receipts"
        ]
        self.assertEqual([r["id"] for r in choices], [self.receipt.id])
        result = self.preview_receipt(6)
        old = self.receipt.date
        self.receipt.date += timedelta(days=2)
        self.assertEqual(
            [c["quantity"] for c in result["cells"]],
            [c["quantity"] for c in self.calendar()["rows"][0]["cells"]],
        )
        self.receipt.date = old

    def test_74_workflow_simulation_input(self):
        token = self.receipt_choice()["token"]
        for ident, date, error in [
            (True, self.calendar()["dates"][0], ValidationError),
            (self.delivery.id, self.calendar()["dates"][0], AccessError),
            (self.receipt.id, "invalid", ValidationError),
        ]:
            with self.assertRaises(error):
                self.api.erpintegrators_simulate_receipt(
                    self.warehouse.id, self.product.id, ident, date, token, 7
                )
        with self.assertRaises(AccessError):
            self.api.erpintegrators_simulation_choices(self.other_warehouse.id, self.product.id, 7)

    def test_75_workflow_stale_tokens(self):
        token = self.receipt_choice()["token"]
        action_token = self.workflow_row()["token"]
        self.receipt.date += timedelta(days=1)
        with self.assertRaises(ValidationError):
            self.preview_receipt(6, token=token)
        with self.assertRaises(ValidationError):
            self.save_followup(token=action_token)

    def test_76_workflow_native_safety(self):
        models = {
            "stock.move": ["state", "date", "product_uom_qty"],
            "stock.quant": ["quantity", "reserved_quantity"],
            "stock.picking": ["state", "scheduled_date"],
            "mail.message": ["body"],
            "mail.mail": ["state"],
        }
        before = {name: self.env[name].search([]).read(f) for name, f in models.items()}
        self.api.erpintegrators_capture_baseline(self.warehouse.id, [self.product.id], 7)
        self.save_followup()
        self.preview_receipt(6)
        self.assertEqual(
            before, {name: self.env[name].search([]).read(f) for name, f in models.items()}
        )

    def test_77_workflow_group_denial(self):
        api = self.api.with_user(self.basic_user)
        for method, arguments in [
            ("erpintegrators_capture_baseline", [self.warehouse.id, [self.product.id], 7]),
            ("erpintegrators_workflow_owners", []),
            ("erpintegrators_simulation_choices", [self.warehouse.id, self.product.id, 7]),
        ]:
            with self.assertRaises(AccessError):
                getattr(api, method)(*arguments)

    def test_78_workflow_future_receipt(self):
        self.receipt.date += timedelta(days=60)
        result = self.preview_receipt(1)
        self.assertEqual(result["cells"][1]["delta"], 5)
        self.assertEqual(result["cells"][0]["delta"], 0)

    def test_79_workflow_bounds_and_timezone(self):
        self.api.erpintegrators_capture_baseline(self.warehouse.id, [self.product.id], 7)
        baseline = self.calendar()
        self.assertEqual(
            baseline,
            self.api.with_context(tz="Pacific/Auckland").erpintegrators_shortage_calendar(
                self.warehouse.id, [self.product.id], 7
            ),
        )
        with self.assertRaises(ValidationError):
            self.api.erpintegrators_capture_baseline(self.warehouse.id, [], 7)

    def test_80_workflow_fractional_units(self):
        kg = self.env.ref("uom.product_uom_kgm")
        gram = self.env.ref("uom.product_uom_gram")
        product = self.env["product.product"].create(
            {
                "name": "Workflow fractional kilograms",
                "type": "consu",
                "is_storable": True,
                "uom_id": kg.id,
                "uom_po_id": kg.id,
            }
        )
        self.env["stock.quant"]._update_available_quantity(
            product, self.warehouse.lot_stock_id, 0.006
        )
        move = self.make_move(product, 0.006, 4, incoming=True)
        move.write({"product_uom": gram.id, "product_uom_qty": 6})
        choice = self.api.erpintegrators_simulation_choices(self.warehouse.id, product.id, 7)[
            "receipts"
        ][0]
        result = self.api.erpintegrators_simulate_receipt(
            self.warehouse.id, product.id, move.id, self.calendar()["dates"][6], choice["token"], 7
        )
        self.assertAlmostEqual(result["quantity"], move.product_qty)
        self.assertAlmostEqual(move.product_qty, 0.01)
        move.date += timedelta(days=2)
        actual = self.calendar([product.id])["rows"][0]["cells"]
        self.assertEqual([c["quantity"] for c in result["cells"]], [c["quantity"] for c in actual])
        self.assertEqual(result["cells"][4]["quantity"], 0.01)

    def test_82_workflow_source_bound(self):
        values = {
            "name": "Workflow bounded source",
            "product_id": self.product.id,
            "product_uom": self.product.uom_id.id,
            "product_uom_qty": 1,
            "company_id": self.company.id,
            "state": "confirmed",
            "location_id": self.env.ref("stock.stock_location_suppliers").id,
            "location_dest_id": self.warehouse.lot_stock_id.id,
            "date": datetime.utcnow() + timedelta(days=1),
        }
        self.env["stock.move"].create([dict(values) for unused in range(5001)])
        result = self.api.erpintegrators_capture_baseline(self.warehouse.id, [self.product.id], 7)
        self.assertFalse(result["sources_complete"])
        self.assertFalse(self.workflow_row()["comparison"]["sources_complete"])
        self.assertEqual(self.env["erpintegrators.shortage.source"].search_count([]), 5000)

    def test_83_workflow_private_baseline_warehouse_access(self):
        self.api.erpintegrators_capture_baseline(self.warehouse.id, [self.product.id], 7)
        self.save_followup()
        other = self.stock_user.copy({"login": "workflow_other_fixture"})
        self.assertEqual(
            self.api.with_user(other).erpintegrators_shortage_calendar(
                self.warehouse.id, [self.product.id], 7
            )["rows"][0]["workflow"]["comparison"]["status"],
            "no_baseline",
        )
        hidden = self.env["erpintegrators.shortage.snapshot"].with_user(self.stock_user).search([])
        self.env["ir.rule"].create(
            {
                "name": "Workflow warehouse restriction",
                "model_id": self.env["ir.model"]._get_id("stock.warehouse"),
                "domain_force": "[('id','!=',%s)]" % self.warehouse.id,
            }
        )
        for name in ["snapshot", "action", "history"]:
            self.assertFalse(
                self.env["erpintegrators.shortage." + name].with_user(self.stock_user).search([])
            )
        with self.assertRaises(AccessError):
            hidden.read(["basis"])
        with self.assertRaises(AccessError):
            self.workflow_row()

    def test_84_workflow_changed_unit_source_coverage(self):
        self.api.erpintegrators_capture_baseline(self.warehouse.id, [self.product.id], 7)
        snapshot = self.env["erpintegrators.shortage.snapshot"].search([])
        basis = dict(snapshot.basis, unit=-1)
        snapshot._write_internal({"basis": basis})
        self.receipt.product_uom_qty = 7
        comparison = self.workflow_row()["comparison"]
        self.assertEqual(comparison["status"], "incompatible")
        self.assertFalse(comparison["source_changes"])
        self.assertFalse(comparison["sources_complete"])

    def complete_scan(self):
        run = self.api.erpintegrators_scan_start(self.warehouse.id, 7)
        while run["state"] == "running":
            run = self.api.erpintegrators_scan_step(self.warehouse.id, run["id"])
        self.assertEqual(run["state"], "complete")
        return run

    def scan_queue(self, run=False, queue="scan", offset=0, limit=25):
        return self.api.erpintegrators_shortage_queue(
            self.warehouse.id, 7, queue, offset, limit, run["id"] if run else False
        )

    def test_90_scan_catalogue_and_checkpoints(self):
        products = self.env["product.product"].create(
            [
                {"name": "Scan catalogue %s" % i, "type": "consu", "is_storable": True}
                for i in range(205)
            ]
        )
        run = self.api.erpintegrators_scan_start(self.warehouse.id, 7)
        self.assertGreaterEqual(run["total"], len(products) + 2)
        candidates = self.env["erpintegrators.shortage.scan"].browse(run["id"]).candidate_ids
        self.assertIn(self.product.id, candidates)
        self.assertIn(self.empty.id, candidates)
        self.assertNotIn(self.foreign_product.id, candidates)
        self.assertNotIn(self.service.id, candidates)
        self.assertEqual(run["id"], self.api.erpintegrators_scan_start(self.warehouse.id, 7)["id"])
        first = self.api.erpintegrators_scan_step(self.warehouse.id, run["id"])
        self.assertEqual(first["processed"], 100)
        self.assertEqual(first["state"], "running")
        final = self.complete_scan()
        self.assertEqual(final["processed"], final["total"])
        self.assertEqual(self.api.erpintegrators_scan_step(self.warehouse.id, run["id"]), final)

    def test_91_scan_private_candidates_and_owner(self):
        run = self.complete_scan()
        model = self.env["erpintegrators.shortage.scan"].with_user(self.stock_user)
        with self.assertRaises(AccessError):
            model.browse(run["id"]).read(["candidate_ids"])
        other = self.env["stock.warehouse"].with_user(self.basic_user)
        with self.assertRaises(AccessError):
            other.erpintegrators_scan_current(self.warehouse.id, 7)
        with self.assertRaises(AccessError):
            other.erpintegrators_scan_step(self.warehouse.id, run["id"])
        other_user = self.stock_user.copy({"login": "scan_other", "name": "Other stock reviewer"})
        with self.assertRaises(AccessError):
            self.api.with_user(other_user).erpintegrators_scan_step(self.warehouse.id, run["id"])
        self.assertFalse(model.with_user(other_user).search([("id", "=", run["id"])]))

    def test_92_scan_atomic_failure_and_retry(self):
        run = self.api.erpintegrators_scan_start(self.warehouse.id, 7)
        with patch.object(
            type(self.api), "_workflow_collect", side_effect=UserError("Synthetic batch failure")
        ):
            failed = self.api.erpintegrators_scan_step(self.warehouse.id, run["id"])
        self.assertEqual((failed["state"], failed["processed"]), ("failed", 0))
        self.assertEqual(self.scan_queue(failed)["total"], 0)
        retry = self.api.erpintegrators_scan_control(self.warehouse.id, run["id"], "retry")
        self.assertEqual(retry["id"], run["id"])
        self.assertEqual(self.complete_scan()["state"], "complete")

    def test_93_scan_cancel_horizon_and_restart(self):
        run = self.api.erpintegrators_scan_start(self.warehouse.id, 7)
        with self.assertRaises(UserError):
            self.api.erpintegrators_scan_start(self.warehouse.id, 14)
        cancelled = self.api.erpintegrators_scan_control(self.warehouse.id, run["id"], "cancel")
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(
            self.api.erpintegrators_scan_step(self.warehouse.id, run["id"])["processed"], 0
        )
        self.assertNotEqual(
            self.api.erpintegrators_scan_start(self.warehouse.id, 14)["id"], run["id"]
        )

    def test_94_scan_date_rollover(self):
        run = self.api.erpintegrators_scan_start(self.warehouse.id, 7)
        tomorrow = datetime.utcnow() + timedelta(days=1, minutes=1)
        with freeze_time(tomorrow):
            with self.assertRaises(AccessError):
                self.api.erpintegrators_scan_step(self.warehouse.id, run["id"])
        # A date change within 24 hours stops continuation without processing a batch.
        with freeze_time(datetime.utcnow().replace(hour=23, minute=59)):
            run = self.api.erpintegrators_scan_start(self.warehouse.id, 7)
        with freeze_time(datetime.utcnow().replace(hour=23, minute=59) + timedelta(minutes=2)):
            stopped = self.api.erpintegrators_scan_step(self.warehouse.id, run["id"])
            self.assertEqual(stopped["state"], "failed")
            with self.assertRaises(UserError):
                self.api.erpintegrators_scan_control(self.warehouse.id, run["id"], "retry")

    def test_95_scan_retention_direct_read(self):
        run = self.complete_scan()
        model = self.env["erpintegrators.shortage.scan"]
        model.browse(run["id"])._write_internal(
            {"started_at": fields.Datetime.now() - timedelta(hours=25)}
        )
        with self.assertRaises(AccessError):
            model.with_user(self.stock_user).browse(run["id"]).read(["state"])
        self.assertFalse(model.with_user(self.stock_user).search([("id", "=", run["id"])]))
        model._cron_prune()
        self.assertFalse(model.browse(run["id"]).exists())
        self.assertFalse(
            self.env["erpintegrators.shortage.scan.line"].search([("run_id", "=", run["id"])])
        )

    def test_96_scan_native_parity_and_units(self):
        before = self.env["stock.move"].search([]).read(["date", "state", "product_uom_qty"])
        run = self.complete_scan()
        queue = self.scan_queue(run)
        row = next(row for row in queue["rows"] if row["id"] == self.product.id)
        native = self.api.erpintegrators_shortage_calendar(self.warehouse.id, [self.product.id], 7)[
            "rows"
        ][0]
        for field in ["cells", "first_shortage", "worst_deficit", "uom"]:
            self.assertEqual(row[field], native[field])
        self.assertFalse(any(r["id"] == self.empty.id for r in queue["rows"]))
        self.assertEqual(
            before, self.env["stock.move"].search([]).read(["date", "state", "product_uom_qty"])
        )
        self.assertTrue(row["observed_at"])

    def test_97_scan_paging_stability(self):
        products = self.env["product.product"].create(
            [
                {"name": "Scan shortage %s" % i, "type": "consu", "is_storable": True}
                for i in range(60)
            ]
        )
        for product in products:
            self.env["stock.quant"]._update_available_quantity(
                product, self.warehouse.lot_stock_id, -1
            )
        run = self.complete_scan()
        pages = [self.scan_queue(run, offset=n)["rows"] for n in (0, 25, 50)]
        ids = [r["id"] for page in pages for r in page]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(set(products.ids).issubset(ids))
        self.assertEqual(self.scan_queue(run)["rows"], pages[0])
        self.assertLess(len(self.scan_queue(run, offset=9999)["rows"]), 26)

    def test_98_scan_current_product_rule(self):
        run = self.complete_scan()
        line = self.env["erpintegrators.shortage.scan.line"].search(
            [("run_id", "=", run["id"]), ("product_id", "=", self.product.id)]
        )
        self.env["ir.rule"].create(
            {
                "name": "Hide scanned product",
                "model_id": self.env["ir.model"]._get_id("product.product"),
                "domain_force": "[('id', '!=', %s)]" % self.product.id,
            }
        )
        self.assertFalse(any(r["id"] == self.product.id for r in self.scan_queue(run)["rows"]))
        with self.assertRaises(AccessError):
            line.with_user(self.stock_user).read(["observation"])

    def test_99_scan_warehouse_company_scope(self):
        run = self.complete_scan()
        with self.assertRaises(AccessError):
            self.api.erpintegrators_scan_step(self.other_warehouse.id, run["id"])
        self.env["ir.rule"].create(
            {
                "name": "Hide scanned warehouse",
                "model_id": self.env["ir.model"]._get_id("stock.warehouse"),
                "domain_force": "[('id', '!=', %s)]" % self.warehouse.id,
            }
        )
        self.assertFalse(
            self.env["erpintegrators.shortage.scan.line"]
            .with_user(self.stock_user)
            .search([("run_id", "=", run["id"])])
        )
        with self.assertRaises(AccessError):
            self.api.erpintegrators_scan_current(self.warehouse.id, 7)

    def test_100_scan_guarded_storage(self):
        self.complete_scan()
        for name in ("erpintegrators.shortage.scan", "erpintegrators.shortage.scan.line"):
            model = self.env[name].with_user(self.stock_user)
            with self.assertRaises(AccessError):
                model.create({})
            rows = model.search([])
            with self.assertRaises(AccessError):
                rows.write({})
            with self.assertRaises(AccessError):
                rows.unlink()
        with self.assertRaises(AccessError):
            self.env["erpintegrators.shortage.scan"].with_user(self.stock_user)._cron_advance()

    def test_101_scan_cron_requester_permissions(self):
        self.api.erpintegrators_scan_start(self.warehouse.id, 7)
        self.env["erpintegrators.shortage.scan"]._cron_advance()
        self.assertEqual(
            self.api.erpintegrators_scan_current(self.warehouse.id, 7)["state"], "complete"
        )
        next_run = self.api.erpintegrators_scan_start(self.warehouse.id, 7)
        self.stock_user.active = False
        self.env["erpintegrators.shortage.scan"]._cron_advance()
        self.assertEqual(
            self.env["erpintegrators.shortage.scan"].browse(next_run["id"]).state, "failed"
        )

    def test_102_scan_unavailable_after_start(self):
        self.api.erpintegrators_scan_start(self.warehouse.id, 7)
        self.empty.active = False
        result = self.complete_scan()
        self.assertEqual(result["unavailable"], 1)
        self.assertEqual(result["processed"], result["total"])

    def test_103_queues_without_scan(self):
        self.save_followup()
        self.assertEqual(self.scan_queue(queue="my")["total"], 1)
        self.assertEqual(self.scan_queue(queue="overdue")["total"], 0)
        self.assertEqual(self.scan_queue(queue="unassigned")["total"], 0)
        action = self.env["erpintegrators.shortage.action"].search(
            [("product_id", "=", self.product.id)]
        )
        action._write_internal({"due_date": datetime.utcnow().date() - timedelta(days=1)})
        self.assertEqual(self.scan_queue(queue="overdue")["total"], 1)
        action._write_internal({"state": "done"})
        self.assertEqual(self.scan_queue(queue="my")["total"], 0)
        self.assertEqual(self.scan_queue(queue="overdue")["total"], 0)

    def test_104_unassigned_action_membership(self):
        run = self.complete_scan()
        self.assertEqual(self.scan_queue(run, "unassigned")["total"], 1)
        self.save_followup()
        self.assertEqual(self.scan_queue(run, "unassigned")["total"], 0)
        action = self.env["erpintegrators.shortage.action"].search(
            [("product_id", "=", self.product.id)]
        )
        action._write_internal({"state": "done"})
        self.assertEqual(self.scan_queue(run, "unassigned")["total"], 1)
        action._write_internal({"state": "progress", "owner_id": self.basic_user.id})
        self.assertEqual(self.scan_queue(run, "unassigned")["total"], 1)

    def test_105_queues_recovered_still_open(self):
        self.save_followup()
        self.env["stock.quant"]._update_available_quantity(
            self.product, self.warehouse.lot_stock_id, 100
        )
        queue = self.scan_queue(queue="my")
        self.assertEqual(queue["total"], 1)
        self.assertFalse(queue["rows"][0]["first_shortage"])
        self.assertTrue(queue["rows"][0]["workflow"]["action"]["needs_review"])

    def test_106_scan_input_validation(self):
        for args in [(True, 7), (self.warehouse.id, True), (self.warehouse.id, 100)]:
            with self.assertRaises(ValidationError):
                self.api.erpintegrators_scan_start(*args)
        for queue, offset, limit in [
            ("unknown", 0, 25),
            ("my", -1, 25),
            ("my", True, 25),
            ("my", 0, 500),
        ]:
            with self.assertRaises(ValidationError):
                self.api.erpintegrators_shortage_queue(self.warehouse.id, 7, queue, offset, limit)

    def test_107_scan_changed_unit_label(self):
        run = self.complete_scan()
        unit = self.env["uom.uom"].search(
            [
                ("category_id", "=", self.product.uom_id.category_id.id),
                ("id", "!=", self.product.uom_id.id),
            ],
            limit=1,
        )
        self.env["erpintegrators.shortage.scan.line"].search(
            [("run_id", "=", run["id"]), ("product_id", "=", self.product.id)]
        )._write_internal({"unit_id": unit.id})
        row = next(r for r in self.scan_queue(run)["rows"] if r["id"] == self.product.id)
        self.assertTrue(row["unit_changed"])

    def test_108_scan_snapshot_membership(self):
        run = self.api.erpintegrators_scan_start(self.warehouse.id, 7)
        added = self.env["product.product"].create(
            {"name": "Created after scan start", "type": "consu", "is_storable": True}
        )
        self.env["stock.quant"]._update_available_quantity(added, self.warehouse.lot_stock_id, -5)
        self.complete_scan()
        self.assertFalse(any(r["id"] == added.id for r in self.scan_queue(run)["rows"]))
        self.assertTrue(
            any(r["id"] == added.id for r in self.scan_queue(self.complete_scan())["rows"])
        )

    def test_109_scan_cron_definition(self):
        worker = self.env.ref("erpintegrators_daily_stock_matrix.shortage_scan_worker")
        self.assertTrue(worker.active)
        self.assertEqual((worker.interval_number, worker.interval_type), (1, "minutes"))
        self.assertTrue(worker.code.endswith("_cron_advance()"))
        if "numbercall" in worker._fields:
            self.assertEqual(worker.numbercall, -1)
            self.assertEqual(
                self.env.ref(
                    "erpintegrators_daily_stock_matrix.workflow_metadata_cleanup"
                ).numbercall,
                -1,
            )
        cleanup = self.env.ref("erpintegrators_daily_stock_matrix.shortage_scan_cleanup")
        self.assertEqual((cleanup.interval_number, cleanup.interval_type), (1, "hours"))
        if "numbercall" in cleanup._fields:
            self.assertEqual(cleanup.numbercall, -1)

    def test_110_scan_failure_preserves_checkpoint(self):
        products = self.env["product.product"].create(
            [
                {"name": "Checkpoint item %s" % i, "type": "consu", "is_storable": True}
                for i in range(105)
            ]
        )
        for product in products[:3]:
            self.env["stock.quant"]._update_available_quantity(
                product, self.warehouse.lot_stock_id, -1
            )
        run = self.api.erpintegrators_scan_start(self.warehouse.id, 7)
        first = self.api.erpintegrators_scan_step(self.warehouse.id, run["id"])
        before = self.scan_queue(first)["rows"]
        with patch.object(
            type(self.api),
            "_workflow_collect",
            side_effect=UserError("Synthetic second batch failure"),
        ):
            failed = self.api.erpintegrators_scan_step(self.warehouse.id, run["id"])
        self.assertEqual((failed["state"], failed["processed"]), ("failed", 100))
        self.assertEqual(self.scan_queue(failed)["rows"], before)
        self.api.erpintegrators_scan_control(self.warehouse.id, run["id"], "retry")
        final = self.complete_scan()
        self.assertEqual(final["processed"], final["total"])
        self.assertEqual(
            len({r["id"] for r in self.scan_queue(final)["rows"]}), self.scan_queue(final)["total"]
        )

    def test_111_scan_empty_catalogue(self):
        self.env["ir.rule"].create(
            {
                "name": "Empty catalogue for reviewer",
                "model_id": self.env["ir.model"]._get_id("product.product"),
                "domain_force": "[('id', '=', -1)]",
            }
        )
        run = self.api.erpintegrators_scan_start(self.warehouse.id, 7)
        self.assertEqual((run["state"], run["total"], run["processed"]), ("complete", 0, 0))
        self.assertEqual(self.scan_queue(run)["total"], 0)

    def test_112_scan_no_outgoing_messages(self):
        before = {
            name: self.env[name].search_count([])
            for name in [
                "mail.message",
                "mail.mail",
                "stock.move",
                "stock.picking",
                "stock.move.line",
            ]
        }
        run = self.complete_scan()
        for queue in ["scan", "my", "overdue", "unassigned"]:
            self.scan_queue(run, queue)
        after = {name: self.env[name].search_count([]) for name in before}
        self.assertEqual(before, after)

    def test_113_scan_bounded_batch_query(self):
        products = self.env["product.product"].create(
            [
                {"name": "Bounded item %s" % i, "type": "consu", "is_storable": True}
                for i in range(250)
            ]
        )
        self.api.erpintegrators_scan_start(self.warehouse.id, 7)
        seen = []
        original = type(self.api)._workflow_collect

        def capture(api, warehouse, ids, days):
            seen.append(len(ids))
            return original(api, warehouse, ids, days)

        started = time.monotonic()
        with patch.object(type(self.api), "_workflow_collect", capture):
            final = self.complete_scan()
        self.assertEqual(sum(seen), final["total"])
        self.assertTrue(all(0 < n <= 100 for n in seen))
        self.assertGreaterEqual(len(seen), 3)
        self.assertGreaterEqual(final["total"], len(products))
        logging.getLogger(__name__).info(
            "SCAN_BENCHMARK products=%s batches=%s seconds=%.3f",
            final["total"],
            len(seen),
            time.monotonic() - started,
        )

    def test_114_scan_restart_after_expiry(self):
        run = self.api.erpintegrators_scan_start(self.warehouse.id, 7)
        old = self.env["erpintegrators.shortage.scan"].browse(run["id"])
        old._write_internal({"started_at": fields.Datetime.now() - timedelta(hours=25)})
        new = self.api.erpintegrators_scan_start(self.warehouse.id, 7)
        self.assertNotEqual(new["id"], old.id)
        self.assertEqual(old.state, "failed")
        self.assertFalse(old.active_key)
        self.assertEqual(new["state"], "running")

    def test_115_scan_start_retries_a_stale_creation_snapshot(self):
        original = self.api.erpintegrators_scan_start(self.warehouse.id, 7)
        model = self.env["erpintegrators.shortage.scan"]
        with patch.object(type(model), "search", return_value=model.browse()):
            with self.assertRaises(SerializationFailure):
                with self.env.cr.savepoint():
                    self.api.erpintegrators_scan_start(self.warehouse.id, 7)
        current = self.api.erpintegrators_scan_current(self.warehouse.id, 7)
        self.assertEqual(current["id"], original["id"])
        self.assertEqual(current["state"], "running")
