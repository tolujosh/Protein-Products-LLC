from odoo import fields, models, api, _
from odoo.tests import Form
from odoo.tools import float_compare, float_round, float_is_zero, format_datetime
from odoo.exceptions import UserError, ValidationError
from odoo.tools.misc import OrderedSet, format_date
from collections import defaultdict


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


    def action_serial_mass_produce_wizard(self):
        self.ensure_one()
        self._check_company()
        if self.state != 'confirmed':
            return
        if self.product_id.tracking != 'serial':
            return
        
        
        next_serial = self.env['stock.production.lot']._get_next_serial(self.company_id, self.product_id)
        action = self.env["ir.actions.actions"]._for_xml_id("mrp.act_assign_serial_numbers_production")
        action['context'] = {
            'default_production_id': self.id,
            'default_expected_qty': self.product_qty,
            'default_next_serial_number': next_serial,
            'default_next_serial_count': self.product_qty - self.qty_produced,
        }
        return action
    
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

    def automate_mark_done(self):
        self.automation_qty = self.product_qty
        for _ in range(self.automation_qty):
            for order in self.procurement_group_id.mrp_production_ids:
                if order.state != 'done':
                    # order.action_generate_move_line_serial_numbers()
                    order.sudo().button_mark_done()
               
    def action_generate_move_line_serial_numbers(self):
        for line in self.move_raw_ids:
            move = self.env['stock.move'].browse(line.id)
            # move.auto_generate_move_line_sequence()

    

class ExtendStockMove(models.Model):
    _inherit = 'stock.move'


    def auto_generate_move_line_sequence(self):
        count = 0
        to_consume_qty = self.raw_material_production_id.product_qty
        ordered_qty = self.product_uom_qty
        qty = ordered_qty / to_consume_qty
        for i in self.move_line_ids:
            count+=1
            if count < qty + 1:
                i.lot_id = self.env['stock.production.lot'].create({'name':f'{self.raw_material_production_id.name}#{count}',
                                                                    'product_id':i.product_id.id,
                                                                'company_id':self.env.company.id})
                i.qty_done = 1
                self.quantity_done = count
            
