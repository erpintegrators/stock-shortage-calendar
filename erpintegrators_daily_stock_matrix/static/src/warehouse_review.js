/** @odoo-module **/
import { Component, onWillStart, onPatched, onWillDestroy, proxy, useProps, t } from '@odoo/owl';
import { useBus, useService } from '@web/core/utils/hooks';
import { _t } from '@web/core/l10n/translation';

export class WarehouseReview extends Component {
    static template = 'erpintegrators_daily_stock_matrix.WarehouseReview';
    props = useProps({ warehouseId: t.number(), days: t.number(), onOpen: t.function() });
    setup() {
        this.orm = useService('orm');
        this.alive = true;
        this.queueRequest = 0;
        this.pendingFocus = null;
        onPatched(() => this.restorePageFocus());
        this.state = proxy({
            run: null,
            queue: 'scan',
            data: null,
            busy: false,
            loading: false,
            error: '',
        });
        onWillStart(() => this.load());
        onWillDestroy(() => {
            this.alive = false;
            ++this.queueRequest;
            clearTimeout(this.timer);
        });
        useBus(this.env.bus, 'erpintegrators-shortage-action-updated', () => this.loadQueue());
    }
    async call(method, args) {
        return this.orm.call('stock.warehouse', method, args);
    }
    schedule() {
        clearTimeout(this.timer);
        if (this.alive && this.state.run?.state === 'running' && !this.state.error) {
            this.timer = setTimeout(() => this.advance(), 1000);
        }
    }
    fail(error) {
        this.state.error =
            error.data?.message || error.message || _t('The warehouse review is unavailable.');
        clearTimeout(this.timer);
    }
    async load() {
        this.state.error = '';
        this.state.busy = true;
        try {
            const run = await this.call('erpintegrators_scan_current', [
                this.props.warehouseId,
                this.props.days,
            ]);
            if (!this.alive) return;
            this.state.run = run;
            await this.loadQueue();
        } catch (error) {
            if (this.alive) this.fail(error);
        } finally {
            if (this.alive) {
                this.state.busy = false;
                this.schedule();
            }
        }
    }
    async start() {
        this.state.error = '';
        this.state.busy = true;
        try {
            const run = await this.call('erpintegrators_scan_start', [
                this.props.warehouseId,
                this.props.days,
            ]);
            if (!this.alive) return;
            this.state.run = run;
            await this.loadQueue();
        } catch (error) {
            if (this.alive) this.fail(error);
        } finally {
            if (this.alive) {
                this.state.busy = false;
                this.schedule();
            }
        }
    }
    async advance() {
        if (this.state.busy || !this.alive || !this.state.run) return;
        this.state.busy = true;
        try {
            const run = await this.call('erpintegrators_scan_step', [
                this.props.warehouseId,
                this.state.run.id,
            ]);
            if (!this.alive) return;
            this.state.run = run;
            await this.loadQueue(this.state.data?.offset || 0);
        } catch (error) {
            if (this.alive) this.fail(error);
        } finally {
            if (this.alive) {
                this.state.busy = false;
                this.schedule();
            }
        }
    }
    async control(operation) {
        clearTimeout(this.timer);
        this.state.busy = true;
        this.state.error = '';
        try {
            const run = await this.call('erpintegrators_scan_control', [
                this.props.warehouseId,
                this.state.run.id,
                operation,
            ]);
            if (!this.alive) return;
            this.state.run = run;
            await this.loadQueue();
        } catch (error) {
            if (this.alive) this.fail(error);
        } finally {
            if (this.alive) {
                this.state.busy = false;
                this.schedule();
            }
        }
    }
    async changeQueue(event) {
        this.state.queue = event.target.value;
        await this.loadQueue();
    }
    async loadQueue(offset = 0) {
        const sequence = ++this.queueRequest;
        this.state.data = null;
        this.state.loading = true;
        try {
            const result = await this.call('erpintegrators_shortage_queue', [
                this.props.warehouseId,
                this.props.days,
                this.state.queue,
                offset,
                25,
                this.state.run?.id || false,
            ]);
            if (this.alive && sequence === this.queueRequest) this.state.data = result;
        } catch (error) {
            if (this.alive && sequence === this.queueRequest) this.fail(error);
        } finally {
            if (this.alive && sequence === this.queueRequest) this.state.loading = false;
        }
    }
    restorePageFocus() {
        const pending = this.pendingFocus;
        if (!pending || this.state.loading || !this.state.data) return;
        this.pendingFocus = null;
        if (!this.alive || pending.sequence !== this.queueRequest) return;
        const selector = pending.direction > 0 ? '.o_queue_next' : '.o_queue_previous';
        const button = pending.root.querySelector(selector);
        if (button && !button.disabled) button.focus();
        else pending.root.querySelector('.o_scan_queue')?.focus();
    }
    async page(event, direction) {
        this.pendingFocus = {
            root: event.currentTarget.closest('.o_warehouse_review'),
            sequence: this.queueRequest + 1,
            direction,
        };
        await this.loadQueue(Math.max(0, this.state.data.offset + direction));
    }
    open(id) {
        return this.props.onOpen(id);
    }
}
