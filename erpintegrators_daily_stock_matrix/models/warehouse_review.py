from datetime import datetime, timedelta

from psycopg2.errors import UniqueViolation

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


class ShortageScan(models.Model):
    _name = "erpintegrators.shortage.scan"
    _inherit = "erpintegrators.workflow.storage"
    _description = "Private warehouse shortage scan"
    _order = "id desc"

    user_id = fields.Many2one("res.users", required=True, ondelete="cascade")
    company_id = fields.Many2one("res.company", required=True, ondelete="cascade")
    warehouse_id = fields.Many2one("stock.warehouse", required=True, ondelete="cascade")
    days = fields.Integer(required=True)
    business_date = fields.Date(required=True)
    started_at = fields.Datetime(required=True)
    updated_at = fields.Datetime(required=True)
    completed_at = fields.Datetime()
    state = fields.Selection(
        [(v, v.title()) for v in ("running", "complete", "failed", "cancelled")], required=True
    )
    candidate_ids = fields.Json(groups="base.group_system")
    checkpoint = fields.Integer()
    total = fields.Integer()
    unavailable = fields.Integer()
    failure = fields.Char()
    active_key = fields.Char(index=True)
    _sql_constraints = [
        ("active_scan_unique", "unique(active_key)", "A warehouse scan is already running.")
    ]

    @api.model
    def _search(self, domain, *args, **kwargs):
        if not self.env.su:
            domain = list(domain) + [
                ("started_at", ">=", fields.Datetime.now() - timedelta(hours=24))
            ]
        return super()._search(domain, *args, **kwargs)

    def check_access_rule(self, operation):
        super().check_access_rule(operation)
        if not self.env.su and any(
            run.started_at < fields.Datetime.now() - timedelta(hours=24) for run in self
        ):
            raise AccessError(_("This scan has expired. Start a new scan."))

    @api.model
    def _cron_advance(self):
        if not self.env.su:
            raise AccessError(_("The scheduled worker requires the maintenance account."))
        for run in self.search([("state", "=", "running")], order="updated_at, id", limit=5):
            try:
                with self.env.cr.savepoint():
                    if not run.user_id.active:
                        raise AccessError(_("The requester is unavailable."))
                    worker = (
                        self.env["stock.warehouse"]
                        .with_user(run.user_id)
                        .with_context(allowed_company_ids=[run.company_id.id])
                    )
                    worker.erpintegrators_scan_step(run.warehouse_id.id, run.id)
            except (AccessError, UserError, ValidationError):
                run._write_internal(
                    {
                        "state": "failed",
                        "active_key": False,
                        "failure": "access",
                        "updated_at": fields.Datetime.now(),
                    }
                )

    @api.model
    def _cron_prune(self):
        if not self.env.su:
            raise AccessError(_("Scan cleanup requires the maintenance account."))
        self.search(
            [("started_at", "<", fields.Datetime.now() - timedelta(hours=24))]
        )._unlink_internal()


class ShortageScanLine(models.Model):
    _name = "erpintegrators.shortage.scan.line"
    _inherit = "erpintegrators.workflow.storage"
    _description = "Observed warehouse shortage"
    _order = "first_shortage, product_id, id"

    run_id = fields.Many2one(
        "erpintegrators.shortage.scan", required=True, ondelete="cascade", index=True
    )
    product_id = fields.Many2one("product.product", required=True, ondelete="cascade")
    unit_id = fields.Many2one("uom.uom", required=True, ondelete="restrict")
    observed_at = fields.Datetime(required=True)
    first_shortage = fields.Date(required=True)
    observation = fields.Json(required=True)
    _sql_constraints = [
        (
            "scan_product_unique",
            "unique(run_id, product_id)",
            "This product was already observed in the scan.",
        )
    ]


class StockWarehouse(models.Model):
    _inherit = "stock.warehouse"

    @api.model
    def _scan_scope(self, warehouse_id, days=14):
        company = self._erpintegrators_calendar_access()
        if (
            type(warehouse_id) is not int
            or warehouse_id < 1
            or type(days) is not int
            or days not in (7, 14, 31)
        ):
            raise ValidationError(_("Choose a warehouse and 7, 14 or 31 days."))
        warehouse = self.with_context(allowed_company_ids=[company.id]).search(
            [("id", "=", warehouse_id), ("company_id", "=", company.id)]
        )
        if not warehouse:
            raise AccessError(_("The warehouse is unavailable in the active company."))
        return company, warehouse

    @api.model
    def _scan_products(self, company, ids=None):
        return (
            self.env["product.product"]
            .with_context(allowed_company_ids=[company.id])
            .search(
                [
                    ("active", "=", True),
                    ("type", "=", "product"),
                    ("company_id", "in", [False, company.id]),
                ]
                + ([("id", "in", ids)] if ids is not None else []),
                order="id",
            )
        )

    @api.model
    def _scan_run(self, warehouse_id, run_id):
        company, warehouse = self._scan_scope(warehouse_id)
        if type(run_id) is not int or run_id < 1:
            raise ValidationError(_("Choose a valid scan."))
        run = self.env["erpintegrators.shortage.scan"].search(
            [
                ("id", "=", run_id),
                ("user_id", "=", self.env.uid),
                ("company_id", "=", company.id),
                ("warehouse_id", "=", warehouse.id),
                ("started_at", ">=", fields.Datetime.now() - timedelta(hours=24)),
            ]
        )
        if not run:
            raise AccessError(_("This scan is unavailable or has expired. Start a new scan."))
        return run

    @api.model
    def _scan_status(self, run):
        return {
            "id": run.id,
            "days": run.days,
            "state": run.state,
            "processed": run.checkpoint,
            "total": run.total,
            "unavailable": run.unavailable,
            "business_date": fields.Date.to_string(run.business_date),
            "started_at": fields.Datetime.to_string(run.started_at),
            "updated_at": fields.Datetime.to_string(run.updated_at),
            "completed_at": (
                fields.Datetime.to_string(run.completed_at) if run.completed_at else False
            ),
            "failure": run.failure or "",
            "date_changed": run.business_date != datetime.utcnow().date(),
        }

    @api.model
    def erpintegrators_scan_current(self, warehouse_id, days=14):
        company, warehouse = self._scan_scope(warehouse_id, days)
        run = self.env["erpintegrators.shortage.scan"].search(
            [
                ("user_id", "=", self.env.uid),
                ("company_id", "=", company.id),
                ("warehouse_id", "=", warehouse.id),
                ("days", "=", days),
                ("started_at", ">=", fields.Datetime.now() - timedelta(hours=24)),
            ],
            limit=1,
        )
        return self._scan_status(run) if run else False

    @api.model
    def erpintegrators_scan_start(self, warehouse_id, days=14):
        company, warehouse = self._scan_scope(warehouse_id, days)
        key = "%s:%s:%s" % (self.env.uid, company.id, warehouse.id)
        self._workflow_lock("scan-start:" + key)
        model = self.env["erpintegrators.shortage.scan"]
        active = model.sudo().search(
            [
                ("active_key", "=", key),
                ("user_id", "=", self.env.uid),
                ("company_id", "=", company.id),
                ("warehouse_id", "=", warehouse.id),
            ]
        )
        if active:
            if (
                active.business_date != datetime.utcnow().date()
                or active.started_at < fields.Datetime.now() - timedelta(hours=24)
            ):
                active._write_internal(
                    {
                        "state": "failed",
                        "active_key": False,
                        "failure": "date",
                        "updated_at": fields.Datetime.now(),
                    }
                )
                active.flush_recordset(["active_key"])
            elif active.days == days:
                return self._scan_status(active.with_env(self.env))
            else:
                raise UserError(
                    _(
                        "A scan with another horizon is running. Return to that horizon and cancel it first."
                    )
                )
        candidates = self._scan_products(company).ids
        stamp = fields.Datetime.now()
        # Requester and warehouse scope are checked before private metadata storage.
        try:
            with self.env.cr.savepoint():
                run = (
                    model.sudo()
                    ._create_internal(
                        {
                            "user_id": self.env.uid,
                            "company_id": company.id,
                            "warehouse_id": warehouse.id,
                            "days": days,
                            "business_date": datetime.utcnow().date(),
                            "started_at": stamp,
                            "updated_at": stamp,
                            "state": "running" if candidates else "complete",
                            "candidate_ids": candidates,
                            "total": len(candidates),
                            "active_key": key if candidates else False,
                            "completed_at": False if candidates else stamp,
                        }
                    )
                    .with_env(self.env)
                )
                run.flush_recordset()
        except UniqueViolation as error:
            if error.diag.constraint_name != "erpintegrators_shortage_scan_active_scan_unique":
                raise
            # A waiting transaction can retain an older repeatable-read snapshot.
            self.env.cr.execute(
                "DO $$ BEGIN RAISE EXCEPTION 'Concurrent warehouse scan start' "
                "USING ERRCODE = '40001'; END $$"
            )
        return self._scan_status(run)

    @api.model
    def erpintegrators_scan_step(self, warehouse_id, run_id):
        run = self._scan_run(warehouse_id, run_id)
        self.env.cr.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", [1700313, run.id])
        if not self.env.cr.fetchone()[0]:
            return self._scan_status(run)
        run.invalidate_recordset()
        if run.state != "running":
            return self._scan_status(run)
        if run.business_date != datetime.utcnow().date():
            run._write_internal(
                {
                    "state": "failed",
                    "active_key": False,
                    "failure": "date",
                    "updated_at": fields.Datetime.now(),
                }
            )
            return self._scan_status(run)
        candidates = run.sudo().candidate_ids
        batch = candidates[run.checkpoint : run.checkpoint + 100]
        try:
            with self.env.cr.savepoint():
                products = self._scan_products(run.company_id, batch)
                stamp = fields.Datetime.now()
                if products:
                    calendar, bases, company, warehouse, products = self._workflow_collect(
                        warehouse_id, products.ids, run.days
                    )
                    model = self.env["erpintegrators.shortage.scan.line"]
                    for row in calendar["rows"]:
                        if row["first_shortage"]:
                            model._create_internal(
                                {
                                    "run_id": run.id,
                                    "product_id": row["id"],
                                    "unit_id": bases[row["id"]]["unit"],
                                    "observed_at": stamp,
                                    "first_shortage": row["first_shortage"],
                                    "observation": row,
                                }
                            )
                cursor = run.checkpoint + len(batch)
                finished = cursor >= run.total
                run._write_internal(
                    {
                        "checkpoint": cursor,
                        "unavailable": run.unavailable + len(batch) - len(products),
                        "updated_at": stamp,
                        "state": "complete" if finished else "running",
                        "completed_at": stamp if finished else False,
                        "active_key": False if finished else run.active_key,
                        "failure": False,
                    }
                )
        except (AccessError, UserError, ValidationError):
            run._write_internal(
                {
                    "state": "failed",
                    "active_key": False,
                    "failure": "batch",
                    "updated_at": fields.Datetime.now(),
                }
            )
        return self._scan_status(run)

    @api.model
    def erpintegrators_scan_control(self, warehouse_id, run_id, operation):
        run = self._scan_run(warehouse_id, run_id)
        self._workflow_lock(
            "scan-start:%s:%s:%s" % (self.env.uid, run.company_id.id, run.warehouse_id.id)
        )
        self.env.cr.execute("SELECT pg_advisory_xact_lock(%s, %s)", [1700313, run.id])
        run.invalidate_recordset()
        if operation == "cancel":
            if run.state == "running":
                run._write_internal(
                    {"state": "cancelled", "active_key": False, "updated_at": fields.Datetime.now()}
                )
        elif operation == "retry":
            if run.business_date != datetime.utcnow().date():
                raise UserError(_("The UTC date changed. Start a new scan."))
            key = "%s:%s:%s" % (self.env.uid, run.company_id.id, run.warehouse_id.id)
            if self.env["erpintegrators.shortage.scan"].search_count(
                [("active_key", "=", key), ("id", "!=", run.id)]
            ):
                raise UserError(_("Another scan is running. Cancel it before retrying this scan."))
            if run.state == "failed":
                run._write_internal(
                    {
                        "state": "running",
                        "active_key": key,
                        "failure": False,
                        "updated_at": fields.Datetime.now(),
                    }
                )
        else:
            raise ValidationError(_("Choose retry or cancel."))
        return self._scan_status(run)

    @api.model
    def erpintegrators_shortage_queue(
        self, warehouse_id, days=14, queue="scan", offset=0, limit=25, scan_id=False
    ):
        company, warehouse = self._scan_scope(warehouse_id, days)
        if (
            queue not in ("scan", "my", "overdue", "unassigned")
            or type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or limit not in (10, 25, 50)
        ):
            raise ValidationError(_("Choose a valid queue and page."))
        actions = self.env["erpintegrators.shortage.action"].with_context(
            allowed_company_ids=[company.id]
        )
        domain = [
            ("company_id", "=", company.id),
            ("warehouse_id", "=", warehouse.id),
            ("state", "in", ["open", "progress"]),
        ]
        run = False
        if queue in ("my", "overdue"):
            domain += (
                [("owner_id", "=", self.env.uid)]
                if queue == "my"
                else [("due_date", "<", datetime.utcnow().date())]
            )
            total = actions.search_count(domain)
            offset = min(offset, max(0, ((total - 1) // limit) * limit))
            page = actions.search(
                domain, order="due_date, product_id, id", offset=offset, limit=limit
            )
            rows = (
                self.erpintegrators_shortage_calendar(
                    warehouse_id, page.mapped("product_id").ids, days
                )["rows"]
                if page
                else []
            )
            by_id = {r["id"]: r for r in rows}
            rows = [
                dict(by_id[a.product_id.id], observed_at=False, unit_changed=False) for a in page
            ]
        else:
            if not scan_id:
                return {
                    "total": 0,
                    "offset": 0,
                    "limit": limit,
                    "rows": [],
                    "scan": False,
                    "queue": queue,
                }
            run = self._scan_run(warehouse_id, scan_id)
            if run.days != days:
                raise UserError(_("The selected scan uses another horizon."))
            line_domain = [("run_id", "=", run.id)]
            if queue == "unassigned":
                eligible = [u["id"] for u in self.erpintegrators_workflow_owners()]
                assigned = (
                    actions.search(domain + [("owner_id", "in", eligible)]).mapped("product_id").ids
                )
                line_domain += [("product_id", "not in", assigned)]
            lines = self.env["erpintegrators.shortage.scan.line"]
            total = lines.search_count(line_domain)
            offset = min(offset, max(0, ((total - 1) // limit) * limit))
            page = lines.search(
                line_domain, order="first_shortage, product_id, id", offset=offset, limit=limit
            )
            rows = [
                dict(
                    line.observation,
                    name=line.product_id.display_name,
                    observed_at=fields.Datetime.to_string(line.observed_at),
                    unit_changed=line.unit_id != line.product_id.uom_id,
                )
                for line in page
            ]
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "rows": rows,
            "scan": self._scan_status(run) if run else False,
            "queue": queue,
        }
