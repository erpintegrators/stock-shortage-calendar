Stock Shortage Calendar
=======================

Solvync Inc.

Review warehouse shortages, explain native supply and demand, compare saved
baselines, assign follow-up ownership and preview supplier receipt timing.
My actions, Overdue and Unassigned help the team return to its next action.
Warehouse scans run in resumable batches when the host scheduler is enabled.

Installation
------------

Choose the release matching the Odoo major version. Extract the archive into
the custom addons path, update the apps list and install Stock Shortage Calendar.
Inventory user access is required. The same module supports Community and
Enterprise. Self-hosted Odoo and suitable Odoo.sh environments are supported.
Odoo Online cannot install this Python module.

Public release names end in .0.1. Internal manifest versions end in .0.1.0
to satisfy the version parser in every supported Odoo series. Earlier private
test candidates used higher version numbers; those installations need an
explicit administrator-managed module update rather than automatic ordering.

Operation
---------

Choose one company warehouse, up to 100 calendar products and 7, 14 or 31 UTC
days. Warehouse scans cover the readable stock-tracked catalogue in batches of
100. Scan observations expire after 24 hours. Receipt timing is a simulation;
actual receipt dates and stock remain controlled by native Odoo operations.
The optional protocol connection is outside this release.

Licence and support
-------------------

LGPL-3. See LICENSE and COPYING-GPL. No proprietary Odoo source is included.
Product and training: https://erpintegrators.com/odoo-stock-shortage-calendar/
Installation and support: https://solvync.com/contact/
