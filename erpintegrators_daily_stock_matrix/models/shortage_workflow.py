import hashlib
import json
from datetime import datetime, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.tools.float_utils import float_round

from .stock_enhancements import shortage_metrics


def signature(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def compare_balances(previous, current):
    if (
        previous.get("unit") != current["unit"]
        or previous.get("calculation") != current["calculation"]
    ):
        return {"status": "incompatible", "dates": 0, "changed": True}
    old = {c["date"]: c["quantity"] for c in previous["cells"]}
    new = {c["date"]: c["quantity"] for c in current["cells"]}
    dates = sorted(set(old) & set(new))
    if not dates:
        return {"status": "no_overlap", "dates": 0, "changed": True}
    before = [old[d] for d in dates]
    after = [new[d] for d in dates]
    changed = before != after
    if not any(q < 0 for q in before) and any(q < 0 for q in after):
        status = "new_shortage"
    elif any(q < 0 for q in before) and not any(q < 0 for q in after):
        status = "cleared"
    elif min(after) < min(before) or sum(q < 0 for q in after) > sum(q < 0 for q in before):
        status = "worsening"
    elif min(after) > min(before) or sum(q < 0 for q in after) < sum(q < 0 for q in before):
        status = "improved"
    else:
        status = "changed" if changed else "unchanged"
    return {
        "status": status,
        "dates": len(dates),
        "changed": changed,
        "from": dates[0],
        "through": dates[-1],
    }


class WorkflowStorage(models.AbstractModel):
    _name = "erpintegrators.workflow.storage"
    _description = "Guarded shortage workflow storage"

    @api.model
    def _workflow_references(self):
        return {
            "erpintegrators.shortage.scan": [("warehouse_id", "stock.warehouse")],
            "erpintegrators.shortage.scan.line": [
                ("run_id", "erpintegrators.shortage.scan"),
                ("product_id", "product.product"),
            ],
            "erpintegrators.shortage.snapshot": [
                ("warehouse_id", "stock.warehouse"),
                ("product_id", "product.product"),
            ],
            "erpintegrators.shortage.source": [
                ("snapshot_id", "erpintegrators.shortage.snapshot"),
                ("move_id", "stock.move"),
            ],
            "erpintegrators.shortage.action": [
                ("warehouse_id", "stock.warehouse"),
                ("product_id", "product.product"),
            ],
            "erpintegrators.shortage.history": [("action_id", "erpintegrators.shortage.action")],
        }.get(self._name, [])

    @api.model
    def _search(self, domain, *args, **kwargs):
        if not self.env.su:
            domain = list(domain) + [
                (field, "in", self.env[model]._search([]))
                for field, model in self._workflow_references()
            ]
        return super()._search(domain, *args, **kwargs)

    def _workflow_check_references(self):
        if not self.env.su:
            for field, model in self._workflow_references():
                related = self.sudo().mapped(field).with_env(self.env)
                related.check_access_rights("read")
                related.check_access_rule("read")

    def check_access_rule(self, operation):
        super().check_access_rule(operation)
        self._workflow_check_references()

    @api.model_create_multi
    def create(self, values_list):
        raise AccessError(_("Use the stock calendar controls to save workflow records."))

    def write(self, values):
        raise AccessError(_("Use the stock calendar controls to save workflow records."))

    def unlink(self):
        raise AccessError(_("Use the stock calendar controls to remove workflow records."))

    @api.model
    def _create_internal(self, values):
        return super().create(values)

    def _write_internal(self, values):
        return super().write(values)

    def _unlink_internal(self):
        return super().unlink()


class ShortageSnapshot(models.Model):
    _name = "erpintegrators.shortage.snapshot"
    _inherit = "erpintegrators.workflow.storage"
    _description = "Private shortage review baseline"

    user_id = fields.Many2one("res.users", required=True, ondelete="cascade")
    company_id = fields.Many2one("res.company", required=True, ondelete="cascade")
    warehouse_id = fields.Many2one("stock.warehouse", required=True, ondelete="cascade")
    product_id = fields.Many2one("product.product", required=True, ondelete="cascade")
    captured_at = fields.Datetime(required=True)
    basis = fields.Json(required=True)
    source_complete = fields.Boolean()
    _sql_constraints = [
        (
            "review_scope_unique",
            "unique(user_id, company_id, warehouse_id, product_id)",
            "One baseline is allowed for each review scope.",
        )
    ]

    @api.model
    def _cron_prune(self):
        if not self.env.su:
            raise AccessError(_("Workflow cleanup requires the scheduled maintenance account."))
        self.search(
            [("captured_at", "<", fields.Datetime.now() - timedelta(days=90))]
        )._unlink_internal()
        self.env["erpintegrators.shortage.history"].search(
            [("changed_at", "<", fields.Datetime.now() - timedelta(days=365))]
        )._unlink_internal()


class SnapshotSource(models.Model):
    _name = "erpintegrators.shortage.source"
    _inherit = "erpintegrators.workflow.storage"
    _description = "Accessible baseline source movement"

    snapshot_id = fields.Many2one(
        "erpintegrators.shortage.snapshot", required=True, ondelete="cascade"
    )
    move_id = fields.Many2one("stock.move", required=True, ondelete="cascade")
    facts = fields.Json(required=True)


class ShortageAction(models.Model):
    _name = "erpintegrators.shortage.action"
    _inherit = "erpintegrators.workflow.storage"
    _description = "Stock shortage follow-up"

    company_id = fields.Many2one("res.company", required=True, ondelete="cascade")
    warehouse_id = fields.Many2one("stock.warehouse", required=True, ondelete="cascade")
    product_id = fields.Many2one("product.product", required=True, ondelete="cascade")
    owner_id = fields.Many2one("res.users", required=True, ondelete="restrict")
    due_date = fields.Date(required=True)
    note = fields.Text()
    state = fields.Selection(
        [("open", "Open"), ("progress", "In progress"), ("done", "Done")], required=True
    )
    revision = fields.Integer(required=True, default=1)
    basis = fields.Json(required=True)
    _sql_constraints = [
        (
            "followup_scope_unique",
            "unique(company_id, warehouse_id, product_id)",
            "One current follow-up is allowed for each product and warehouse.",
        )
    ]


class ShortageHistory(models.Model):
    _name = "erpintegrators.shortage.history"
    _inherit = "erpintegrators.workflow.storage"
    _description = "Stock shortage follow-up revision"
    _order = "id desc"

    action_id = fields.Many2one("erpintegrators.shortage.action", required=True, ondelete="cascade")
    actor_id = fields.Many2one("res.users", required=True, ondelete="restrict")
    changed_at = fields.Datetime(required=True)
    revision = fields.Integer(required=True)
    owner_id = fields.Many2one("res.users", required=True, ondelete="restrict")
    due_date = fields.Date(required=True)
    state = fields.Char(required=True)
    note = fields.Text()


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

    @api.model
    def _workflow_round(self, product, quantity):
        return float_round(quantity, precision_rounding=product.uom_id.rounding)

    @api.model
    def _workflow_destination(self, move):
        return move.location_dest_id

    @api.model
    def _workflow_collect(self, warehouse_id, product_ids, days):
        calendar = super().erpintegrators_shortage_calendar(warehouse_id, product_ids, days)
        company, warehouse, products = self._erpintegrators_calendar_selection(
            warehouse_id, product_ids
        )
        by_id = {p.id: p for p in products}
        bases = {
            r["id"]: {
                "calculation": "native-daily-v1",
                "unit": by_id[r["id"]].uom_id.id,
                "cells": [{"date": c["date"], "quantity": c["quantity"]} for c in r["cells"]],
            }
            for r in calendar["rows"]
        }
        return calendar, bases, company, warehouse, products

    @api.model
    def _workflow_lock(self, scope):
        key = int.from_bytes(hashlib.sha256(scope.encode()).digest()[:4], "big", signed=True)
        self.env.cr.execute("SELECT pg_advisory_xact_lock(%s, %s)", [1700312, key])

    @api.model
    def _workflow_move_facts(self, move):
        return {
            "date": fields.Datetime.to_string(move.date),
            "quantity": move.product_qty,
            "state": move.state,
            "source": move.location_id.id,
            "destination": self._workflow_destination(move).id,
        }

    @api.model
    def _workflow_sources(self, company, warehouse, products, last_date):
        domain = self._erpintegrators_movement_domain(company, warehouse, products[:1], last_date)
        domain = [
            (
                ("product_id", "in", products.ids)
                if isinstance(t, tuple) and t[0] == "product_id"
                else t
            )
            for t in domain
        ]
        moves = (
            self.env["stock.move"]
            .with_context(allowed_company_ids=[company.id])
            .search(domain, order="product_id, date, id", limit=5001)
        )
        result = {p.id: [] for p in products}
        for move in moves[:5000]:
            result[move.product_id.id].append((move.id, self._workflow_move_facts(move)))
        return result, len(moves) <= 5000

    @api.model
    def _workflow_source_changes(self, snapshot, current_sources, complete):
        if not snapshot.source_complete or not complete:
            return [], False
        rows = self.env["erpintegrators.shortage.source"].search(
            [("snapshot_id", "=", snapshot.id)]
        )
        current = dict(current_sources)
        changes = []
        for source in rows:
            now = current.get(source.move_id.id)
            if now is None:
                changes.append({"move_id": source.move_id.id, "kind": "no_longer_open"})
            elif source.facts != now:
                changes.append(
                    {
                        "move_id": source.move_id.id,
                        "kind": "changed",
                        "old_date": source.facts["date"],
                        "date": now["date"],
                        "old_quantity": source.facts["quantity"],
                        "quantity": now["quantity"],
                    }
                )
        known = set(rows.mapped("move_id").ids)
        for ident, facts in current_sources:
            if ident not in known:
                changes.append(
                    {
                        "move_id": ident,
                        "kind": "added",
                        "date": facts["date"],
                        "quantity": facts["quantity"],
                    }
                )
        return changes[:10], len(changes) <= 10

    @api.model
    def erpintegrators_shortage_calendar(self, warehouse_id, product_ids, days=14):
        calendar, bases, company, warehouse, products = self._workflow_collect(
            warehouse_id, product_ids, days
        )
        context = dict(self.env.context, allowed_company_ids=[company.id])
        snapshots = (
            self.env["erpintegrators.shortage.snapshot"]
            .with_context(context)
            .search(
                [
                    ("user_id", "=", self.env.uid),
                    ("company_id", "=", company.id),
                    ("warehouse_id", "=", warehouse.id),
                    ("product_id", "in", products.ids),
                    ("captured_at", ">=", fields.Datetime.now() - timedelta(days=90)),
                ]
            )
        )
        saved = {s.product_id.id: s for s in snapshots}
        sources, complete = (
            self._workflow_sources(company, warehouse, products, calendar["dates"][-1])
            if snapshots
            else ({}, True)
        )
        actions = (
            self.env["erpintegrators.shortage.action"]
            .with_context(context)
            .search(
                [
                    ("company_id", "=", company.id),
                    ("warehouse_id", "=", warehouse.id),
                    ("product_id", "in", products.ids),
                ]
            )
        )
        followups = {a.product_id.id: a for a in actions}
        history = (
            self.env["erpintegrators.shortage.history"]
            .with_context(context)
            .search([("action_id", "in", actions.ids)], limit=1000)
        )
        for row in calendar["rows"]:
            basis = bases[row["id"]]
            snapshot = saved.get(row["id"])
            review = (
                compare_balances(snapshot.basis, basis)
                if snapshot
                else {"status": "no_baseline", "dates": 0, "changed": False}
            )
            review["captured_at"] = (
                fields.Datetime.to_string(snapshot.captured_at) if snapshot else False
            )
            review["source_changes"], review["sources_complete"] = (
                self._workflow_source_changes(snapshot, sources.get(row["id"], []), complete)
                if snapshot
                else ([], True)
            )
            if review["status"] == "incompatible":
                review["source_changes"], review["sources_complete"] = [], False
            action = followups.get(row["id"])
            action_data = False
            if action:
                action_data = {
                    "id": action.id,
                    "owner_id": action.owner_id.id,
                    "owner": action.owner_id.display_name,
                    "due_date": fields.Date.to_string(action.due_date),
                    "note": action.note or "",
                    "state": action.state,
                    "revision": action.revision,
                    "needs_review": compare_balances(action.basis, basis)["changed"],
                    "history": [
                        {
                            "revision": h.revision,
                            "at": fields.Datetime.to_string(h.changed_at),
                            "actor": h.actor_id.display_name,
                            "owner": h.owner_id.display_name,
                            "due_date": fields.Date.to_string(h.due_date),
                            "state": h.state,
                            "note": h.note or "",
                        }
                        for h in history.filtered(lambda h: h.action_id == action)[:10]
                    ],
                }
            row["workflow"] = {
                "token": signature(basis),
                "comparison": review,
                "action": action_data,
                "forecast_short": bool(row["first_shortage"]),
            }
        return calendar

    @api.model
    def erpintegrators_capture_baseline(self, warehouse_id, product_ids, days=14):
        calendar, bases, company, warehouse, products = self._workflow_collect(
            warehouse_id, product_ids, days
        )
        self._workflow_lock("baseline:%s:%s:%s" % (self.env.uid, company.id, warehouse.id))
        sources, complete = self._workflow_sources(
            company, warehouse, products, calendar["dates"][-1]
        )
        model = self.env["erpintegrators.shortage.snapshot"].with_context(
            allowed_company_ids=[company.id]
        )
        stamp = fields.Datetime.now()
        for product in products:
            snapshot = model.search(
                [
                    ("user_id", "=", self.env.uid),
                    ("company_id", "=", company.id),
                    ("warehouse_id", "=", warehouse.id),
                    ("product_id", "=", product.id),
                ],
                limit=1,
            )
            values = {
                "user_id": self.env.uid,
                "company_id": company.id,
                "warehouse_id": warehouse.id,
                "product_id": product.id,
                "captured_at": stamp,
                "basis": bases[product.id],
                "source_complete": complete,
            }
            if snapshot:
                snapshot._write_internal(values)
                self.env["erpintegrators.shortage.source"].sudo().search(
                    [("snapshot_id", "=", snapshot.id)]
                )._unlink_internal()
            else:
                snapshot = model._create_internal(values)
            details = [
                {"snapshot_id": snapshot.id, "move_id": ident, "facts": facts}
                for ident, facts in sources[product.id]
            ]
            if details:
                self.env["erpintegrators.shortage.source"]._create_internal(details)
        return {
            "captured": len(products),
            "captured_at": fields.Datetime.to_string(stamp),
            "sources_complete": complete,
        }

    @api.model
    def erpintegrators_workflow_owners(self):
        company = self._erpintegrators_calendar_access()
        users = self.env["res.users"].search(
            [("active", "=", True), ("share", "=", False), ("company_ids", "in", [company.id])]
        )
        return [
            {"id": u.id, "name": u.display_name}
            for u in users
            if u.with_user(u).has_group("stock.group_stock_user")
        ]

    @api.model
    def erpintegrators_save_shortage_action(
        self, warehouse_id, product_id, days, token, revision, owner_id, due_date, state, note
    ):
        calendar, bases, company, warehouse, products = self._workflow_collect(
            warehouse_id, [product_id], days
        )
        if (
            type(revision) is not int
            or revision < 0
            or type(owner_id) is not int
            or owner_id not in [u["id"] for u in self.erpintegrators_workflow_owners()]
            or state not in ["open", "progress", "done"]
            or not isinstance(note, str)
            or len(note) > 2000
        ):
            raise ValidationError(
                _("Choose an eligible owner, a valid state and a note of up to 2000 characters.")
            )
        try:
            valid_date = fields.Date.to_date(due_date)
        except (ValueError, TypeError):
            valid_date = False
        if (
            not isinstance(due_date, str)
            or not valid_date
            or fields.Date.to_string(valid_date) != due_date
        ):
            raise ValidationError(_("Choose a valid due date."))
        if not isinstance(token, str) or token != signature(bases[product_id]):
            raise ValidationError(
                _("The forecast changed. Refresh the calendar before saving the action.")
            )
        self._workflow_lock("action:%s:%s:%s" % (company.id, warehouse.id, product_id))
        model = self.env["erpintegrators.shortage.action"].with_context(
            allowed_company_ids=[company.id]
        )
        model.invalidate_model()
        action = model.search(
            [
                ("company_id", "=", company.id),
                ("warehouse_id", "=", warehouse.id),
                ("product_id", "=", product_id),
            ],
            limit=1,
        )
        if (action.revision if action else 0) != revision:
            raise ValidationError(
                _("This action changed in another view. Reload the calendar before saving.")
            )
        if not action and not calendar["rows"][0]["first_shortage"]:
            raise ValidationError(_("Create a follow-up for a product with a current shortage."))
        values = {
            "company_id": company.id,
            "warehouse_id": warehouse.id,
            "product_id": product_id,
            "owner_id": owner_id,
            "due_date": valid_date,
            "state": state,
            "note": note,
            "revision": revision + 1,
            "basis": bases[product_id],
        }
        if action:
            action._write_internal(values)
        else:
            action = model._create_internal(values)
        self.env["erpintegrators.shortage.history"]._create_internal(
            {
                "action_id": action.id,
                "actor_id": self.env.uid,
                "changed_at": fields.Datetime.now(),
                "revision": action.revision,
                "owner_id": owner_id,
                "due_date": valid_date,
                "state": state,
                "note": note,
            }
        )
        self.env["erpintegrators.shortage.history"].search(
            [("action_id", "=", action.id)], offset=50
        )._unlink_internal()
        return {"revision": action.revision}

    @api.model
    def _workflow_receipt_domain(self, company, warehouse, product):
        domain = self._erpintegrators_movement_domain(
            company, warehouse, product, fields.Date.to_string(datetime.utcnow().date())
        )
        return [t for t in domain if not (isinstance(t, tuple) and t[0] == "date")] + [
            ("location_id.usage", "=", "supplier"),
            ("product_qty", ">", 0),
        ]

    @api.model
    def _workflow_receipt_token(self, basis, move):
        return signature(
            {"basis": basis, "move_id": move.id, "facts": self._workflow_move_facts(move)}
        )

    @api.model
    def _workflow_raw_balances(self, company, warehouse, product, dates):
        groups = (
            self.env["report.stock.quantity"]
            .with_context(allowed_company_ids=[company.id], tz="UTC")
            .read_group(
                [
                    ("company_id", "=", company.id),
                    ("warehouse_id", "=", warehouse.id),
                    ("product_id", "=", product.id),
                    ("state", "=", "forecast"),
                    ("date", ">=", dates[0]),
                    ("date", "<=", dates[-1]),
                ],
                ["product_qty:sum"],
                ["date:day"],
                lazy=False,
            )
        )
        return {g["__range"]["date:day"]["from"][:10]: g["product_qty"] for g in groups}

    @api.model
    def erpintegrators_simulation_choices(self, warehouse_id, product_id, days=14):
        calendar, bases, company, warehouse, products = self._workflow_collect(
            warehouse_id, [product_id], days
        )
        simulation_basis = dict(
            bases[product_id],
            raw=self._workflow_raw_balances(company, warehouse, products, calendar["dates"]),
        )
        moves = (
            self.env["stock.move"]
            .with_context(allowed_company_ids=[company.id])
            .search(
                self._workflow_receipt_domain(company, warehouse, products),
                order="date, id",
                limit=101,
            )
        )
        choices = []
        for move in moves[:100]:
            label = _("Stock movement %s", move.id)
            if move.picking_id:
                try:
                    move.picking_id.check_access_rights("read")
                    move.picking_id.check_access_rule("read")
                    label = move.picking_id.name
                except AccessError:
                    pass
            choices.append(
                {
                    "id": move.id,
                    "name": label,
                    "date": fields.Datetime.to_string(move.date),
                    "quantity": move.product_qty,
                    "token": self._workflow_receipt_token(simulation_basis, move),
                }
            )
        return {
            "product_id": product_id,
            "product": products.display_name,
            "uom": products.uom_id.name,
            "dates": calendar["dates"],
            "receipts": choices,
            "truncated": len(moves) > 100,
        }

    @api.model
    def erpintegrators_simulate_receipt(
        self, warehouse_id, product_id, move_id, new_date, token, days=14
    ):
        calendar, bases, company, warehouse, products = self._workflow_collect(
            warehouse_id, [product_id], days
        )
        raw = self._workflow_raw_balances(company, warehouse, products, calendar["dates"])
        simulation_basis = dict(bases[product_id], raw=raw)
        if (
            type(move_id) is not int
            or move_id < 1
            or not isinstance(new_date, str)
            or new_date not in calendar["dates"]
        ):
            raise ValidationError(
                _("Choose an eligible receipt and a UTC date in the visible horizon.")
            )
        move = (
            self.env["stock.move"]
            .with_context(allowed_company_ids=[company.id])
            .search(
                [("id", "=", move_id)]
                + self._workflow_receipt_domain(company, warehouse, products),
                limit=1,
            )
        )
        if not move:
            raise AccessError(
                _(
                    "The receipt is unavailable or no longer eligible. Refresh the simulation choices."
                )
            )
        if not isinstance(token, str) or token != self._workflow_receipt_token(
            simulation_basis, move
        ):
            raise ValidationError(
                _("The receipt or forecast changed. Refresh the simulation choices.")
            )
        original = fields.Date.to_string(move.date.date())
        quantity = move.product_qty
        cells = []
        for cell in calendar["rows"][0]["cells"]:
            simulated = self._workflow_round(
                products,
                raw.get(cell["date"], 0.0)
                + quantity * (int(cell["date"] >= new_date) - int(cell["date"] >= original)),
            )
            cells.append(
                {
                    "date": cell["date"],
                    "baseline": cell["quantity"],
                    "quantity": simulated,
                    "delta": self._workflow_round(products, simulated - cell["quantity"]),
                    "shortage": simulated < 0,
                }
            )
        return {
            "product": products.display_name,
            "uom": products.uom_id.name,
            "move_id": move.id,
            "original_date": original,
            "new_date": new_date,
            "quantity": quantity,
            "observed_at": fields.Datetime.to_string(fields.Datetime.now()),
            "cells": cells,
            "before": shortage_metrics(calendar["rows"][0]["cells"]),
            "after": shortage_metrics(cells),
        }
