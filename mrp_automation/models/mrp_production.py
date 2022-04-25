from odoo import fields, models, api, _
from odoo.tests import Form
from odoo.tools import float_compare, float_round, float_is_zero, format_datetime
from odoo.exceptions import UserError, ValidationError


class ExtendMrpProduction(models.Model):
    _inherit = 'mrp.production'

    is_mark_done = fields.Boolean('Done', default=False)
    is_visible = fields.Boolean('Visible')
    automation_qty = fields.Integer(string='Automation_qty')
    tracking = fields.Selection(
        string='tracking',
        selection=[('serial', 'serial'), ('lot', 'lot'),('none', 'none')],
        related='product_id.tracking',
        readonly=True,
        store=True)

    
    def generate_production_lot(self, product):
        company_id = self.env.company
        production_lot = self.env['stock.production.lot'].create({
        'product_id': product.id,
        'company_id': company_id.id,
        'name': self.env['stock.production.lot']._get_next_serial(company_id, product) or self.env['ir.sequence'].next_by_code('stock.lot.serial')})
        return production_lot
          
    def generate_bom_serial_numbers(self):
        for raw in self.move_raw_ids:
            for _ in range(int(raw.product_uom_qty)):
                production_lot =  self.generate_production_lot(product=raw.product_id)
                raw.write({'lot_ids':[(6, 0,[production_lot.id])]})

    def _action_generate_backorder_wizard(self, quantity_issues):
        ctx = self.env.context.copy()
        lines = []
        for order in quantity_issues:
            lines.append((0, 0, {
                'mrp_production_id': order.id,
                'to_backorder': True
            }))
        ctx.update({'default_mrp_production_ids': self.ids,
                    'default_mrp_production_backorder_line_ids': lines})
        backorder = Form(
            self.env['mrp.production.backorder'].with_context(ctx))
        backorder.save().action_backorder()

    def _action_generate_immediate_wizard(self):
        ctx = dict(self.env.context, default_mo_ids=[
            (4, mo.id) for mo in self])
        backorder = Form(
            self.env['mrp.immediate.production'].with_context(ctx))
        backorder.save().process()

    def button_mark_done(self):
        self.automation_qty = self.product_qty
        self._button_mark_done_sanity_checks()

        if not self.env.context.get('button_mark_done_production_ids'):
            self = self.with_context(button_mark_done_production_ids=self.ids)
        res = self._pre_button_mark_done()
        if res is not True:
            return res

        if self.env.context.get('mo_ids_to_backorder'):
            productions_to_backorder = self.browse(
                self.env.context['mo_ids_to_backorder'])
            productions_not_to_backorder = self - productions_to_backorder
            close_mo = False
        else:
            productions_not_to_backorder = self
            productions_to_backorder = self.env['mrp.production']
            close_mo = True

        self.workorder_ids.button_finish()

        backorders = productions_to_backorder._generate_backorder_productions(
            close_mo=close_mo)
        productions_not_to_backorder._post_inventory(cancel_backorder=True)
        productions_to_backorder._post_inventory(cancel_backorder=True)

        # if completed products make other confirmed/partially_available moves available, assign them
        done_move_finished_ids = (productions_to_backorder.move_finished_ids |
                                  productions_not_to_backorder.move_finished_ids).filtered(lambda m: m.state == 'done')
        done_move_finished_ids._trigger_assign()

        # Moves without quantity done are not posted => set them as done instead of canceling. In
        # case the user edits the MO later on and sets some consumed quantity on those, we do not
        # want the move lines to be canceled.
        (productions_not_to_backorder.move_raw_ids | productions_not_to_backorder.move_finished_ids).filtered(lambda x: x.state not in ('done', 'cancel')).write({
            'state': 'done',
            'product_uom_qty': 0.0,
        })

        for production in self:
            production.write({
                'date_finished': fields.Datetime.now(),
                'product_qty': production.qty_produced,
                'priority': '0',
                'is_locked': True,
                'state': 'done',
            })

        for workorder in self.workorder_ids.filtered(lambda w: w.state not in ('done', 'cancel')):
            workorder.duration_expected = workorder._get_duration_expected()

        if not backorders:
            if self.env.context.get('from_workorder'):
                return {
                    'type': 'ir.actions.act_window',
                    'res_model': 'mrp.production',
                    'views': [[self.env.ref('mrp.mrp_production_form_view').id, 'form']],
                    'res_id': self.id,
                    'target': 'main',
                }
            return True
        context = self.env.context.copy()
        context = {k: v for k, v in context.items(
        ) if not k.startswith('default_')}
        for k, v in context.items():
            if k.startswith('skip_'):
                context[k] = False
        action = {
            'res_model': 'mrp.production',
            'type': 'ir.actions.act_window',
            'context': dict(context, mo_ids_to_backorder=None, button_mark_done_production_ids=None)
        }
        if len(backorders) == 1:
            action.update({
                'view_mode': 'form',
                'res_id': backorders[0].id,
            })

        else:
            action.update({
                'name': _("Backorder MO"),
                'domain': [('id', 'in', backorders.ids)],
                'view_mode': 'tree,form',
            })
        self.is_mark_done = True

    def button_auto_confirmation(self):
        for _ in range(self.automation_qty):
            if self.is_mark_done == True:
                for order in self.procurement_group_id.mrp_production_ids:
                    if order.state != 'done':
                        order._action_generate_immediate_wizard()
                        order.sudo().button_mark_done()
        self.is_visible = True

    def button_auto_generation(self):
        self.automation_qty = self.product_qty
        raw = self.move_raw_ids.filtered(lambda i: i.product_id.tracking == 'none')
        if len(raw) > 0:
            for _ in range(self.automation_qty):
                for order in self.procurement_group_id.mrp_production_ids:
                    if order.state != 'done':
                        order.generate_bom_serial_numbers()
                        order.sudo().action_generate_serial()
                        order.sudo().button_mark_done()
        else:
            self.button_auto_confirmation()

    def button_mass_generation(self):
        action = self.action_serial_mass_produce_wizard()
        wizard = Form(self.env['stock.assign.serial'].with_context(**action['context']))
        # Let the wizard generate all serial numbers
        action = wizard.save().generate_serial_numbers_production()
        # Reload the wizard to apply generated serial numbers
        wizard = Form(self.env['stock.assign.serial'].browse(action['res_id']))
        wizard.save().apply()