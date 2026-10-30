/** @odoo-module **/
import { Component, onWillStart, useState } from '@odoo/owl';
import { registry } from '@web/core/registry';
import { useService } from '@web/core/utils/hooks';
import { _t } from '@web/core/l10n/translation';
import { WarehouseReview } from './warehouse_review';

export class StockShortageCalendar extends Component {
    static template = 'erpintegrators_daily_stock_matrix.Calendar';
    static components = { WarehouseReview };
    setup() {
        this.orm = useService('orm');
        this.action = useService('action');
        this.request = 0;
        this.choicesRequest = 0;
        this.explanationRequest = 0;
        this.watchRequest = 0;
        this.state = useState({
            loading: false,
            searching: false,
            error: '',
            search: '',
            warehouse: '',
            days: 14,
            warehouses: [],
            products: [],
            selected: [],
            data: null,
            more: false,
            sort: 'date',
            watchBusy: false,
            watchMessage: '',
            explanation: null,
            explanationBusy: false,
            explanationError: '',
            explanationProduct: null,
            explanationDate: '',
        });
        this.workflowRequest = 0;
        this.simulationRequest = 0;
        Object.assign(this.state, {
            workflowBusy: false,
            workflowMessage: '',
            workflowError: '',
            actionForm: null,
            owners: [],
            simulationProduct: null,
            simulationChoices: null,
            simulationReceipt: '',
            simulationDate: '',
            simulationBusy: false,
            simulationError: '',
            preview: null,
        });
        onWillStart(() => this.loadChoices(true));
    }
    async loadChoices(initial = false) {
        this.invalidate();
        const request = ++this.choicesRequest;
        this.state.searching = true;
        this.state.error = '';
        try {
            const result = await this.orm.call(
                'stock.warehouse',
                'erpintegrators_calendar_choices',
                [this.state.search],
            );
            if (request !== this.choicesRequest) return;
            this.state.warehouses = result.warehouses;
            this.state.products = result.products;
            this.state.more = result.more_products;
            this.state.selected = initial ? result.products.slice(0, 20).map((row) => row.id) : [];
            this.state.data = null;
            if (!result.warehouses.some((row) => String(row.id) === String(this.state.warehouse))) {
                this.state.warehouse = result.warehouses[0]?.id || '';
            }
            if (initial && this.state.warehouse && this.state.selected.length) await this.refresh();
        } catch (error) {
            if (request === this.choicesRequest)
                this.state.error =
                    error.data?.message || error.message || _t('The product search failed.');
        } finally {
            if (request === this.choicesRequest) this.state.searching = false;
        }
    }
    invalidate() {
        this.invalidateWorkflow();
        ++this.request;
        ++this.explanationRequest;
        ++this.watchRequest;
        this.state.data = null;
        this.state.loading = false;
        this.state.explanation = null;
        this.state.explanationBusy = false;
        this.state.explanationError = '';
        this.state.explanationProduct = null;
        this.state.watchBusy = false;
        this.state.watchMessage = '';
    }
    changeWarehouse(event) {
        this.invalidate();
        this.state.warehouse = Number(event.target.value);
    }
    changeDays(event) {
        this.invalidate();
        this.state.days = Number(event.target.value);
    }
    toggle(event) {
        this.invalidate();
        const id = Number(event.target.value);
        this.state.selected = event.target.checked
            ? [...this.state.selected, id]
            : this.state.selected.filter((value) => value !== id);
    }
    get noShortageLabel() {
        return _t('None in range');
    }
    get recoveryUnknownLabel() {
        return _t('No recovery within visible horizon');
    }
    get worklist() {
        const rows = (this.state.data?.rows || []).filter((row) => row.first_shortage);
        return [...rows].sort((a, b) => {
            if (this.state.sort === 'name') return a.name.localeCompare(b.name);
            if (this.state.sort === 'duration' && a.shortage_days !== b.shortage_days)
                return b.shortage_days - a.shortage_days;
            return a.first_shortage.localeCompare(b.first_shortage) || a.name.localeCompare(b.name);
        });
    }
    changeSort(event) {
        this.state.sort = event.target.value;
    }
    changeExplanationDate(event) {
        return this.explain(this.state.explanationProduct, event.target.value);
    }
    async saveWatchlist() {
        const request = ++this.watchRequest;
        this.state.watchBusy = true;
        this.state.watchMessage = '';
        this.state.error = '';
        try {
            await this.orm.call('stock.warehouse', 'erpintegrators_save_watchlist', [
                Number(this.state.warehouse),
                [...this.state.selected],
            ]);
            if (request === this.watchRequest) this.state.watchMessage = _t('Watchlist saved.');
        } catch (error) {
            if (request === this.watchRequest)
                this.state.error = error.data?.message || error.message;
        } finally {
            if (request === this.watchRequest) this.state.watchBusy = false;
        }
    }
    async restoreWatchlist() {
        this.invalidate();
        ++this.choicesRequest;
        const request = ++this.watchRequest;
        this.state.watchBusy = true;
        this.state.error = '';
        this.state.searching = false;
        try {
            const saved = await this.orm.call(
                'stock.warehouse',
                'erpintegrators_load_watchlist',
                [],
            );
            if (request !== this.watchRequest) return;
            if (!saved.exists) {
                this.state.watchMessage = _t('No saved watchlist in this company.');
                return;
            }
            this.state.products = saved.products;
            this.state.selected = saved.products.map((row) => row.id);
            this.state.search = '';
            this.state.more = false;
            this.state.watchMessage = saved.unavailable
                ? _t('Watchlist restored. Some products are unavailable.')
                : _t('Watchlist restored.');
            if (this.state.selected.length && this.state.warehouse) await this.refresh();
        } catch (error) {
            if (request === this.watchRequest)
                this.state.error = error.data?.message || error.message;
        } finally {
            if (request === this.watchRequest) this.state.watchBusy = false;
        }
    }
    async explain(id, date) {
        const request = ++this.explanationRequest;
        this.state.explanation = null;
        this.state.explanationError = '';
        this.state.explanationBusy = true;
        this.state.explanationProduct = id;
        this.state.explanationDate = date;
        try {
            const result = await this.orm.call(
                'stock.warehouse',
                'erpintegrators_explain_shortage',
                [Number(this.state.warehouse), id, date, this.state.days],
            );
            if (request === this.explanationRequest) this.state.explanation = result;
        } catch (error) {
            if (request === this.explanationRequest)
                this.state.explanationError = error.data?.message || error.message;
        } finally {
            if (request === this.explanationRequest) this.state.explanationBusy = false;
        }
    }
    async openMovement(id) {
        const request = this.explanationRequest;
        try {
            const action = await this.orm.call('stock.warehouse', 'erpintegrators_open_movement', [
                Number(this.state.warehouse),
                this.state.explanationProduct,
                id,
                this.state.days,
            ]);
            if (request === this.explanationRequest) await this.action.doAction(action);
        } catch (error) {
            if (request === this.explanationRequest)
                this.state.explanationError = error.data?.message || error.message;
        }
    }
    searchKey(event) {
        if (event.key === 'Enter') this.loadChoices();
    }
    invalidateWorkflow() {
        ++this.workflowRequest;
        ++this.simulationRequest;
        Object.assign(this.state, {
            workflowBusy: false,
            workflowMessage: '',
            workflowError: '',
            actionForm: null,
            simulationProduct: null,
            simulationChoices: null,
            simulationBusy: false,
            simulationError: '',
            preview: null,
        });
    }
    workflowLabel(status) {
        return (
            {
                no_baseline: _t('No baseline'),
                no_overlap: _t('No comparable dates'),
                incompatible: _t('Baseline units or rules changed'),
                new_shortage: _t('New shortage'),
                worsening: _t('Worsening'),
                improved: _t('Improved'),
                cleared: _t('Cleared on comparable dates'),
                changed: _t('Changed'),
                unchanged: _t('Unchanged'),
            }[status] || status
        );
    }
    async captureBaseline() {
        const seq = ++this.workflowRequest;
        this.state.workflowBusy = true;
        this.state.workflowError = '';
        this.state.workflowMessage = '';
        try {
            const result = await this.orm.call(
                'stock.warehouse',
                'erpintegrators_capture_baseline',
                [Number(this.state.warehouse), [...this.state.selected], this.state.days],
            );
            if (seq !== this.workflowRequest) return;
            const next = this.workflowRequest + 1;
            await this.refresh();
            if (next === this.workflowRequest)
                this.state.workflowMessage = result.sources_complete
                    ? _t('Baseline saved.')
                    : _t('Baseline saved. Source detail is limited.');
        } catch (error) {
            if (seq === this.workflowRequest)
                this.state.workflowError = error.data?.message || error.message;
        } finally {
            if (seq === this.workflowRequest) this.state.workflowBusy = false;
        }
    }
    async openQueueProduct(id) {
        this.invalidate();
        this.state.selected = [id];
        await this.refresh();
        if (!this.state.data?.rows.some((row) => row.id === id)) return;
        const row = this.state.data.rows.find((row) => row.id === id);
        if (!this.state.products.some((product) => product.id === id))
            this.state.products.push({ id, name: row.name });
        await this.editAction(id);
    }
    async editAction(id) {
        const row = this.state.data.rows.find((r) => r.id === id),
            old = row.workflow.action;
        const seq = ++this.workflowRequest;
        this.state.workflowBusy = true;
        this.state.workflowError = '';
        this.state.workflowMessage = '';
        this.state.actionForm = {
            product_id: id,
            product: row.name,
            token: row.workflow.token,
            revision: old?.revision || 0,
            owner: old?.owner_id || '',
            due: old?.due_date || this.state.data.dates[0],
            state: old?.state || 'open',
            note: old?.note || '',
        };
        try {
            const users = await this.orm.call(
                'stock.warehouse',
                'erpintegrators_workflow_owners',
                [],
            );
            if (seq === this.workflowRequest) {
                this.state.owners = users;
                if (!users.some((u) => u.id === Number(this.state.actionForm.owner)))
                    this.state.actionForm.owner = users[0]?.id || '';
            }
        } catch (error) {
            if (seq === this.workflowRequest)
                this.state.workflowError = error.data?.message || error.message;
        } finally {
            if (seq === this.workflowRequest) this.state.workflowBusy = false;
        }
    }
    changeActionOwner(event) {
        this.state.actionForm.owner = Number(event.target.value);
    }
    changeActionDue(event) {
        this.state.actionForm.due = event.target.value;
    }
    changeActionState(event) {
        this.state.actionForm.state = event.target.value;
    }
    changeActionNote(event) {
        this.state.actionForm.note = event.target.value;
    }
    async saveAction() {
        const f = { ...this.state.actionForm },
            seq = ++this.workflowRequest;
        this.state.workflowBusy = true;
        this.state.workflowError = '';
        this.state.workflowMessage = '';
        try {
            await this.orm.call('stock.warehouse', 'erpintegrators_save_shortage_action', [
                Number(this.state.warehouse),
                f.product_id,
                this.state.days,
                f.token,
                f.revision,
                Number(f.owner),
                f.due,
                f.state,
                f.note,
            ]);
            if (seq !== this.workflowRequest) return;
            const next = this.workflowRequest + 1;
            await this.refresh();
            if (next === this.workflowRequest) {
                this.state.workflowMessage = _t('Action saved.');
                this.env.bus.trigger('erpintegrators-shortage-action-updated');
            }
        } catch (error) {
            if (seq === this.workflowRequest)
                this.state.workflowError = error.data?.message || error.message;
        } finally {
            if (seq === this.workflowRequest) this.state.workflowBusy = false;
        }
    }
    async openSimulation(id) {
        const seq = ++this.simulationRequest;
        this.state.simulationProduct = id;
        this.state.simulationBusy = true;
        this.state.simulationChoices = null;
        this.state.preview = null;
        this.state.simulationError = '';
        try {
            const result = await this.orm.call(
                'stock.warehouse',
                'erpintegrators_simulation_choices',
                [Number(this.state.warehouse), id, this.state.days],
            );
            if (seq !== this.simulationRequest) return;
            this.state.simulationChoices = result;
            this.state.simulationReceipt = result.receipts[0]?.id || '';
            const date = result.receipts[0]?.date.slice(0, 10);
            this.state.simulationDate = result.dates.includes(date) ? date : result.dates[0];
        } catch (error) {
            if (seq === this.simulationRequest)
                this.state.simulationError = error.data?.message || error.message;
        } finally {
            if (seq === this.simulationRequest) this.state.simulationBusy = false;
        }
    }
    changeSimulationReceipt(event) {
        ++this.simulationRequest;
        this.state.simulationBusy = false;
        this.state.preview = null;
        this.state.simulationReceipt = Number(event.target.value);
    }
    changeSimulationDate(event) {
        ++this.simulationRequest;
        this.state.simulationBusy = false;
        this.state.preview = null;
        this.state.simulationDate = event.target.value;
    }
    async previewReceipt() {
        const seq = ++this.simulationRequest,
            choice = this.state.simulationChoices.receipts.find(
                (r) => r.id === Number(this.state.simulationReceipt),
            );
        this.state.preview = null;
        this.state.simulationBusy = true;
        this.state.simulationError = '';
        try {
            const result = await this.orm.call(
                'stock.warehouse',
                'erpintegrators_simulate_receipt',
                [
                    Number(this.state.warehouse),
                    this.state.simulationProduct,
                    choice.id,
                    this.state.simulationDate,
                    choice.token,
                    this.state.days,
                ],
            );
            if (seq === this.simulationRequest) this.state.preview = result;
        } catch (error) {
            if (seq === this.simulationRequest)
                this.state.simulationError = error.data?.message || error.message;
        } finally {
            if (seq === this.simulationRequest) this.state.simulationBusy = false;
        }
    }
    async refresh() {
        this.invalidateWorkflow();
        ++this.explanationRequest;
        this.state.explanation = null;
        this.state.explanationProduct = null;
        this.state.explanationError = '';
        this.state.explanationBusy = false;
        const request = ++this.request;
        this.state.loading = true;
        this.state.data = null;
        this.state.error = '';
        try {
            const result = await this.orm.call(
                'stock.warehouse',
                'erpintegrators_shortage_calendar',
                [Number(this.state.warehouse), [...this.state.selected], this.state.days],
            );
            if (request === this.request) this.state.data = result;
        } catch (error) {
            if (request === this.request)
                this.state.error =
                    error.data?.message || error.message || _t('The calendar could not be loaded.');
        } finally {
            if (request === this.request) this.state.loading = false;
        }
    }
    async openForecast(id) {
        try {
            const action = await this.orm.call(
                'stock.warehouse',
                'erpintegrators_calendar_forecast',
                [Number(this.state.warehouse), id],
            );
            await this.action.doAction(action);
        } catch (error) {
            this.state.error = error.data?.message || error.message;
        }
    }
}
registry.category('actions').add('erpintegrators_stock_shortage_calendar', StockShortageCalendar);
