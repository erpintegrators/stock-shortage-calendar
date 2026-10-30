from datetime import datetime, time, timedelta

from odoo import api, fields, models
from odoo.tools.translate import _
from odoo.exceptions import AccessError, ValidationError


def shortage_metrics(cells):
    negative = [index for index, cell in enumerate(cells) if cell["shortage"]]
    recovery = (
        next(
            (index for index in range(negative[0] + 1, len(cells)) if not cells[index]["shortage"]),
            None,
        )
        if negative
        else None
    )
    return {
        "worst_deficit": max([0.0] + [-cell["quantity"] for cell in cells]),
        "shortage_days": len(negative),
        "first_recovery": cells[recovery]["date"] if recovery is not None else False,
        "recurs": recovery is not None and any(index > recovery for index in negative),
    }


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

    @api.model
    def erpintegrators_shortage_calendar(self, warehouse_id, product_ids, days=14):
        result = super().erpintegrators_shortage_calendar(warehouse_id, product_ids, days)
        for row in result["rows"]:
            row.update(shortage_metrics(row["cells"]))
        return result

    @api.model
    def erpintegrators_save_watchlist(self, warehouse_id, product_ids):
        company, warehouse, products = self._erpintegrators_calendar_selection(
            warehouse_id, product_ids
        )
        model = self.env["erpintegrators.stock.watchlist"].with_context(
            allowed_company_ids=[company.id]
        )
        watchlist = model.search(
            [("user_id", "=", self.env.uid), ("company_id", "=", company.id)], limit=1
        )
        values = {"product_ids": products.ids}
        if watchlist:
            watchlist.write(values)
        else:
            model.create(values)
        return {"saved": len(products)}

    @api.model
    def erpintegrators_load_watchlist(self):
        company = self._erpintegrators_calendar_access()
        context = dict(self.env.context, allowed_company_ids=[company.id])
        saved = (
            self.env["erpintegrators.stock.watchlist"]
            .with_context(context)
            .search([("user_id", "=", self.env.uid), ("company_id", "=", company.id)], limit=1)
        )
        ids = saved.product_ids if saved else []
        domain = [
            ("id", "in", ids),
            ("active", "=", True),
            ("is_storable", "=", True),
            ("company_id", "in", [False, company.id]),
        ]
        products = (
            self.env["product.product"]
            .with_context(context)
            .search(domain, order="default_code, name, id")
        )
        return {
            "exists": bool(saved),
            "products": [{"id": p.id, "name": p.display_name} for p in products],
            "unavailable": len(ids) - len(products),
        }

    @api.model
    def _erpintegrators_movement_domain(self, company, warehouse, product, last_date):
        destination = [
            "|",
            "&",
            ("forecasted_location_id", "!=", False),
            ("forecasted_location_id", "child_of", warehouse.view_location_id.id),
            "&",
            ("forecasted_location_id", "=", False),
            ("location_dest_id", "child_of", warehouse.view_location_id.id),
        ]
        source = [("location_id", "child_of", warehouse.view_location_id.id)]
        return [
            ("company_id", "=", company.id),
            ("product_id", "=", product.id),
            ("state", "not in", ["draft", "cancel", "done"]),
            ("product_qty", "!=", 0),
            (
                "date",
                "<",
                datetime.combine(fields.Date.to_date(last_date) + timedelta(days=1), time.min),
            ),
        ] + (["|", "&"] + source + ["!"] + destination + ["&"] + destination + ["!"] + source)

    @api.model
    def erpintegrators_explain_shortage(self, warehouse_id, product_id, date, days=14):
        company, warehouse, product = self._erpintegrators_calendar_selection(
            warehouse_id, [product_id]
        )
        calendar = self.erpintegrators_shortage_calendar(warehouse_id, [product_id], days)
        if not isinstance(date, str) or date not in calendar["dates"]:
            raise ValidationError(_("Choose a UTC date in the visible horizon."))
        row = calendar["rows"][0]
        closing = next(cell["quantity"] for cell in row["cells"] if cell["date"] == date)
        groups = (
            self.env["report.stock.quantity"]
            .with_context(allowed_company_ids=[company.id], tz="UTC")
            ._read_group(
                [
                    ("company_id", "=", company.id),
                    ("warehouse_id", "=", warehouse.id),
                    ("product_id", "=", product.id),
                    ("date", "=", date),
                    ("state", "in", ["in", "out"]),
                ],
                ["state"],
                ["product_qty:sum"],
            )
        )
        totals = {state: quantity for state, quantity in groups}
        incoming = product.uom_id.round(totals.get("in", 0.0))
        outgoing = product.uom_id.round(-totals.get("out", 0.0))
        opening = product.uom_id.round(closing - incoming + outgoing)
        moves = (
            self.env["stock.move"]
            .with_context(allowed_company_ids=[company.id])
            .search(
                self._erpintegrators_movement_domain(
                    company, warehouse, product, calendar["dates"][-1]
                ),
                order="date, id",
                limit=101,
            )
        )
        movements = []
        for move in moves[:100]:
            try:
                destination_path = (
                    move.forecasted_location_id or move.location_dest_id
                ).parent_path
            except AccessError:
                continue
            incoming_move = ("/%s/" % warehouse.view_location_id.id) in (
                "/" + (destination_path or "")
            )
            name = _("Stock movement %s", move.id)
            if move.picking_id:
                try:
                    move.picking_id.check_access("read")
                    name = move.picking_id.name
                except AccessError:
                    pass
            movements.append(
                {
                    "id": move.id,
                    "name": name,
                    "date": fields.Datetime.to_string(move.date),
                    "quantity": product.uom_id.round(move.product_qty),
                    "direction": "in" if incoming_move else "out",
                    "overdue": fields.Date.to_string(move.date.date()) < calendar["dates"][0],
                }
            )
        return {
            "product_id": product.id,
            "product": row["name"],
            "uom": row["uom"],
            "date": date,
            "opening": opening,
            "incoming": incoming,
            "outgoing": outgoing,
            "closing": closing,
            "last_date": calendar["dates"][-1],
            "movements": movements,
            "truncated": len(moves) > 100,
        }

    @api.model
    def erpintegrators_open_movement(self, warehouse_id, product_id, move_id, days=14):
        company, warehouse, product = self._erpintegrators_calendar_selection(
            warehouse_id, [product_id]
        )
        if type(move_id) is not int or move_id < 1:
            raise ValidationError(_("Choose a valid stock movement."))
        calendar = self.erpintegrators_shortage_calendar(warehouse_id, [product_id], days)
        move = (
            self.env["stock.move"]
            .with_context(allowed_company_ids=[company.id])
            .search(
                [("id", "=", move_id)]
                + self._erpintegrators_movement_domain(
                    company, warehouse, product, calendar["dates"][-1]
                ),
                limit=1,
            )
        )
        if not move:
            raise AccessError(_("The movement is unavailable. Refresh the explanation."))
        target = move.picking_id or move
        target.check_access("read")
        return {
            "type": "ir.actions.act_window",
            "res_model": target._name,
            "res_id": target.id,
            "views": [(False, "form")],
            "target": "current",
            "context": dict(self.env.context, allowed_company_ids=[company.id]),
        }


class StockWatchlist(models.Model):
    _name = "erpintegrators.stock.watchlist"
    _description = "Personal stock calendar watchlist"
    _check_company_auto = True

    user_id = fields.Many2one(
        "res.users", required=True, default=lambda self: self.env.user, ondelete="cascade"
    )
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company, ondelete="cascade"
    )
    product_ids = fields.Json(default=list, required=True)
    _owner_company_unique = models.Constraint(
        "UNIQUE(user_id, company_id)", "One watchlist is allowed per user and company."
    )

    @api.model
    def _validate_preference_ids(self, ids):
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= 100
            or any(type(value) is not int or value < 1 for value in ids)
            or len(set(ids)) != len(ids)
        ):
            raise ValidationError(_("Choose between 1 and 100 distinct products."))

    @api.model_create_multi
    def create(self, values_list):
        for values in values_list:
            self._validate_preference_ids(values.get("product_ids", []))
            if (
                values.get("user_id", self.env.uid) != self.env.uid
                or values.get("company_id", self.env.company.id) != self.env.company.id
            ):
                raise AccessError(_("Save a watchlist for yourself in the active company."))
        return super().create(values_list)

    def write(self, values):
        if "user_id" in values or "company_id" in values:
            raise AccessError(_("Watchlist ownership and company cannot be changed."))
        if "product_ids" in values:
            self._validate_preference_ids(values["product_ids"])
        return super().write(values)

    @api.constrains("product_ids", "company_id")
    def _check_products(self):
        for record in self:
            ids = record.product_ids
            self._validate_preference_ids(ids)
            valid = self.env["product.product"].search(
                [
                    ("id", "in", ids),
                    ("active", "=", True),
                    ("is_storable", "=", True),
                    ("company_id", "in", [False, record.company_id.id]),
                ]
            )
            if len(valid) != len(ids):
                raise AccessError(_("A selected product is unavailable in the active company."))
