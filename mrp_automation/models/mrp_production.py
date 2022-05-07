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

    def _split_productions(self, amounts=False, cancel_remaning_qty=False, set_consumed_qty=False):
        """ Splits productions into productions smaller quantities to produce, i.e. creates
        its backorders.
        :param dict amounts: a dict with a production as key and a list value containing
        the amounts each production split should produce including the original production,
        e.g. {mrp.production(1,): [3, 2]} will result in mrp.production(1,) having a product_qty=3
        and a new backorder with product_qty=2.
        :return: mrp.production records in order of [orig_prod_1, backorder_prod_1,
        backorder_prod_2, orig_prod_2, backorder_prod_2, etc.]
        """
        def _default_amounts(production):
            return [production.qty_producing, production._get_quantity_to_backorder()]

        if not amounts:
            amounts = {}
        for production in self:
            mo_amounts = amounts.get(production)
            if not mo_amounts:
                amounts[production] = _default_amounts(production)
                continue
            total_amount = sum(mo_amounts)
            if total_amount < production.product_qty and not cancel_remaning_qty:
                amounts[production].append(production.product_qty - total_amount)
            elif total_amount > production.product_qty or production.state in ['done', 'cancel']:
                raise UserError(_("Unable to split with more than the quantity to produce."))

        backorder_vals_list = []
        initial_qty_by_production = {}

        # Create the backorders.
        for production in self:
            initial_qty_by_production[production] = production.product_qty
            if production.backorder_sequence == 0:  # Activate backorder naming
                production.backorder_sequence = 1
            production.name = self._get_name_backorder(production.name, production.backorder_sequence)
            production.product_qty = amounts[production][0]
            backorder_vals = production.copy_data(default=production._get_backorder_mo_vals())[0]
            backorder_qtys = amounts[production][1:]

            next_seq = max(production.procurement_group_id.mrp_production_ids.mapped("backorder_sequence"), default=1)

            for qty_to_backorder in backorder_qtys:
                next_seq += 1
                backorder_vals_list.append(dict(
                    backorder_vals,
                    product_qty=qty_to_backorder,
                    name=production._get_name_backorder(production.name, next_seq),
                    backorder_sequence=next_seq,
                    state='confirmed'
                ))

        backorders = self.env['mrp.production'].create(backorder_vals_list)

        index = 0
        production_to_backorders = {}
        production_ids = OrderedSet()
        for production in self:
            number_of_backorder_created = len(amounts.get(production, _default_amounts(production))) - 1
            production_backorders = backorders[index:index + number_of_backorder_created]
            production_to_backorders[production] = production_backorders
            production_ids.update(production.ids)
            production_ids.update(production_backorders.ids)
            index += number_of_backorder_created

        # Split the `stock.move` among new backorders.
        new_moves_vals = []
        moves = []
        for production in self:
            for move in production.move_raw_ids | production.move_finished_ids:
                if move.additional:
                    continue
                unit_factor = move.product_uom_qty / initial_qty_by_production[production]
                initial_move_vals = move.copy_data(move._get_backorder_move_vals())[0]
                move.with_context(do_not_unreserve=True).product_uom_qty = production.product_qty * unit_factor

                for backorder in production_to_backorders[production]:
                    move_vals = dict(
                        initial_move_vals,
                        product_uom_qty=backorder.product_qty * unit_factor
                    )
                    if move.raw_material_production_id:
                        move_vals['raw_material_production_id'] = backorder.id
                    else:
                        move_vals['production_id'] = backorder.id
                    new_moves_vals.append(move_vals)
                    moves.append(move)

        backorder_moves = self.env['stock.move'].create(new_moves_vals)
        # Split `stock.move.line`s. 2 options for this:
        # - do_unreserve -> action_assign
        # - Split the reserved amounts manually
        # The first option would be easier to maintain since it's less code
        # However it could be slower (due to `stock.quant` update) and could
        # create inconsistencies in mass production if a new lot higher in a
        # FIFO strategy arrives between the reservation and the backorder creation
        move_to_backorder_moves = defaultdict(lambda: self.env['stock.move'])
        for move, backorder_move in zip(moves, backorder_moves):
            move_to_backorder_moves[move] |= backorder_move

        move_lines_vals = []
        assigned_moves = set()
        partially_assigned_moves = set()
        move_lines_to_unlink = set()

        for initial_move, backorder_moves in move_to_backorder_moves.items():
            ml_by_move = []
            product_uom = initial_move.product_id.uom_id
            for move_line in initial_move.move_line_ids:
                available_qty = move_line.product_uom_id._compute_quantity(move_line.product_uom_qty, product_uom)
                if float_compare(available_qty, 0, precision_rounding=move_line.product_uom_id.rounding) <= 0:
                    continue
                ml_by_move.append((available_qty, move_line, move_line.copy_data()[0]))

            initial_move.move_line_ids.with_context(bypass_reservation_update=True).write({'product_uom_qty': 0})
            moves = list(initial_move | backorder_moves)

            move = moves and moves.pop(0)
            move_qty_to_reserve = move.product_qty
            for quantity, move_line, ml_vals in ml_by_move:
                while float_compare(quantity, 0, precision_rounding=product_uom.rounding) > 0 and move:
                    # Do not create `stock.move.line` if there is no initial demand on `stock.move`
                    taken_qty = min(move_qty_to_reserve, quantity)
                    taken_qty_uom = product_uom._compute_quantity(taken_qty, move_line.product_uom_id)
                    if move == initial_move:
                        move_line.with_context(bypass_reservation_update=True).product_uom_qty = taken_qty_uom
                        if set_consumed_qty:
                            move_line.qty_done = taken_qty_uom
                    elif not float_is_zero(taken_qty_uom, precision_rounding=move_line.product_uom_id.rounding):
                        new_ml_vals = dict(
                            ml_vals,
                            product_uom_qty=taken_qty_uom,
                            move_id=move.id
                        )
                        if set_consumed_qty:
                            new_ml_vals['qty_done'] = taken_qty_uom
                        move_lines_vals.append(new_ml_vals)
                    quantity -= taken_qty
                    move_qty_to_reserve -= taken_qty

                    if float_compare(move_qty_to_reserve, 0, precision_rounding=move.product_uom.rounding) <= 0:
                        assigned_moves.add(move.id)
                        move = moves and moves.pop(0)
                        move_qty_to_reserve = move and move.product_qty or 0

                # Unreserve the quantity removed from initial `stock.move.line` and
                # not assigned to a move anymore. In case of a split smaller than initial
                # quantity and fully reserved
                if quantity:
                    self.env['stock.quant']._update_reserved_quantity(
                        move_line.product_id, move_line.location_id, -quantity,
                        lot_id=move_line.lot_id, package_id=move_line.package_id,
                        owner_id=move_line.owner_id, strict=True)

            if move and move_qty_to_reserve != move.product_qty:
                partially_assigned_moves.add(move.id)

            move_lines_to_unlink.update(initial_move.move_line_ids.filtered(
                lambda ml: not ml.product_uom_qty and not ml.qty_done).ids)

        self.env['stock.move'].browse(assigned_moves).write({'state': 'assigned'})
        self.env['stock.move'].browse(partially_assigned_moves).write({'state': 'partially_available'})
        # Avoid triggering a useless _recompute_state
        self.env['stock.move.line'].browse(move_lines_to_unlink).write({'move_id': False})
        # self.env['stock.move.line'].browse(move_lines_to_unlink).unlink()
        self.env['stock.move.line'].create(move_lines_vals)

        # We need to adapt `duration_expected` on both the original workorders and their
        # backordered workorders. To do that, we use the original `duration_expected` and the
        # ratio of the quantity produced and the quantity to produce.
        for production in self:
            initial_qty = initial_qty_by_production[production]
            initial_workorder_remaining_qty = []
            bo = production_to_backorders[production]

            # Adapt duration
            for workorder in (production | bo).workorder_ids:
                workorder.duration_expected = workorder.duration_expected * workorder.production_id.product_qty / initial_qty

            # Adapt quantities produced
            for workorder in production.workorder_ids:
                initial_workorder_remaining_qty.append(max(workorder.qty_produced - workorder.qty_production, 0))
                workorder.qty_produced = min(workorder.qty_produced, workorder.qty_production)
            workorders_len = len(bo.workorder_ids)
            for index, workorder in enumerate(bo.workorder_ids):
                remaining_qty = initial_workorder_remaining_qty[index // workorders_len]
                if remaining_qty:
                    workorder.qty_produced = max(workorder.qty_production, remaining_qty)
                    initial_workorder_remaining_qty[index % workorders_len] = max(remaining_qty - workorder.qty_produced, 0)
        backorders.workorder_ids._action_confirm()

        return self.env['mrp.production'].browse(production_ids)
    
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

    def automate_mark_done(self):
        self.automation_qty = self.product_qty
        for _ in range(self.automation_qty):
            for order in self.procurement_group_id.mrp_production_ids:
                if order.state != 'done':
                    order.action_generate_move_line_serial_numbers()
                    order.sudo().button_mark_done()
               
    def action_generate_move_line_serial_numbers(self):
        for line in self.move_raw_ids:
            move = self.env['stock.move'].browse(line.id)
            move.auto_generate_move_line_sequence()
    

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
                print("=count==\n\n",count)
                i.lot_id = self.env['stock.production.lot'].create({'name':f'{self.raw_material_production_id.name}#{count}',
                                                                    'product_id':i.product_id.id,
                                                                'company_id':self.env.company.id})
                i.qty_done = 1
                self.quantity_done = count
            