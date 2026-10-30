from datetime import datetime, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools.float_utils import float_compare, float_round


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

    @api.model
    def _erpintegrators_calendar_access(self):
        if not self.env.user.has_group("stock.group_stock_user"):
            raise AccessError(_("Inventory user access is required."))
        return self.env.company

    @api.model
    def _erpintegrators_calendar_selection(self, warehouse_id, product_ids):
        company = self._erpintegrators_calendar_access()
        if type(warehouse_id) is not int or warehouse_id < 1:
            raise ValidationError(_("Choose a valid warehouse."))
        if (
            not isinstance(product_ids, list)
            or not 1 <= len(product_ids) <= 100
            or any(type(value) is not int or value < 1 for value in product_ids)
        ):
            raise ValidationError(_("Choose between 1 and 100 distinct products."))
        product_ids = list(dict.fromkeys(product_ids))
        warehouse = self.with_context(allowed_company_ids=[company.id]).search(
            [
                ("id", "=", warehouse_id),
                ("company_id", "=", company.id),
            ]
        )
        if not warehouse:
            raise AccessError(_("The warehouse is unavailable in the active company."))
        products = (
            self.env["product.product"]
            .with_context(allowed_company_ids=[company.id])
            .search(
                [
                    ("id", "in", product_ids),
                    ("active", "=", True),
                    ("is_storable", "=", True),
                    ("company_id", "in", [False, company.id]),
                ]
            )
        )
        if len(products) != len(product_ids):
            raise AccessError(_("A selected product is unavailable in the active company."))
        return (
            company,
            warehouse,
            products.sorted(key=lambda product: product_ids.index(product.id)),
        )

    @api.model
    def erpintegrators_calendar_choices(self, search=""):
        company = self._erpintegrators_calendar_access()
        if not isinstance(search, str) or len(search) > 120:
            raise ValidationError(_("Use a product search of up to 120 characters."))
        context = dict(self.env.context, allowed_company_ids=[company.id])
        warehouses = self.with_context(context).search([("company_id", "=", company.id)])
        domain = [
            ("active", "=", True),
            ("is_storable", "=", True),
            ("company_id", "in", [False, company.id]),
        ]
        if search:
            domain += ["|", ("name", "ilike", search), ("default_code", "ilike", search)]
        products = (
            self.env["product.product"]
            .with_context(context)
            .search(domain, limit=101, order="default_code, name, id")
        )
        return {
            "company": company.display_name,
            "warehouses": [{"id": row.id, "name": row.display_name} for row in warehouses],
            "products": [{"id": row.id, "name": row.display_name} for row in products[:100]],
            "more_products": len(products) > 100,
        }

    @api.model
    def erpintegrators_shortage_calendar(self, warehouse_id, product_ids, days=14):
        company, warehouse, products = self._erpintegrators_calendar_selection(
            warehouse_id, product_ids
        )
        if type(days) is not int or days not in (7, 14, 31):
            raise ValidationError(_("Choose 7, 14 or 31 days."))
        # This fixed, non-secret setting is the only elevated read.
        period = (
            self.env["ir.config_parameter"]
            .sudo()
            .get_param("stock.report_stock_quantity_period", "3")
        )
        if str(period) != "3":
            raise UserError(_("The calendar requires the native three-month forecast window."))
        # Inspect fixed view metadata because a setting change can outlive its SQL rebuild.
        self.env.cr.execute("SELECT pg_get_viewdef('report_stock_quantity'::regclass, true)")
        definition = self.env.cr.fetchone()[0]
        if "'3 mons'::interval" not in definition:
            raise UserError(
                _("Rebuild the native three-month forecast report before opening the calendar.")
            )
        today = datetime.utcnow().date()
        dates = [fields.Date.to_string(today + timedelta(days=offset)) for offset in range(days)]
        groups = (
            self.env["report.stock.quantity"]
            .with_context(allowed_company_ids=[company.id], tz="UTC")
            .read_group(
                [
                    ("state", "=", "forecast"),
                    ("company_id", "=", company.id),
                    ("warehouse_id", "=", warehouse.id),
                    ("product_id", "in", products.ids),
                    ("date", ">=", dates[0]),
                    ("date", "<=", dates[-1]),
                ],
                ["product_qty:sum"],
                ["product_id", "date:day"],
                lazy=False,
            )
        )
        quantities = {}
        for group in groups:
            date = group["__range"]["date:day"]["from"][:10]
            quantities[(group["product_id"][0], date)] = group["product_qty"]
        rows = []
        for product in products:
            cells = []
            for date in dates:
                quantity = float_round(
                    quantities.get((product.id, date), 0.0),
                    precision_rounding=product.uom_id.rounding,
                )
                negative = (
                    float_compare(quantity, 0.0, precision_rounding=product.uom_id.rounding) < 0
                )
                cells.append({"date": date, "quantity": quantity, "shortage": negative})
            rows.append(
                {
                    "id": product.id,
                    "name": product.display_name,
                    "uom": product.uom_id.name,
                    "first_shortage": next(
                        (cell["date"] for cell in cells if cell["shortage"]), False
                    ),
                    "cells": cells,
                }
            )
        return {
            "company": company.display_name,
            "warehouse": warehouse.display_name,
            "warehouse_id": warehouse.id,
            "timezone": "UTC",
            "dates": dates,
            "rows": rows,
        }

    @api.model
    def erpintegrators_calendar_forecast(self, warehouse_id, product_id):
        company, warehouse, products = self._erpintegrators_calendar_selection(
            warehouse_id, [product_id]
        )
        action = products.action_product_forecast_report()
        action["context"] = dict(
            self.env.context,
            allowed_company_ids=[company.id],
            active_id=products.id,
            active_model="product.product",
            warehouse_id=warehouse.id,
        )
        return action
