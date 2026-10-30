from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    for name in ("workflow_metadata_cleanup", "shortage_scan_worker", "shortage_scan_cleanup"):
        job = env.ref("erpintegrators_daily_stock_matrix." + name, raise_if_not_found=False)
        if job:
            job.write({"numbercall": -1})
