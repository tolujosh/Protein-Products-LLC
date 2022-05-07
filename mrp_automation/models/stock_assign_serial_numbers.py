from odoo import api, fields, models


class StockAssignSerialNumbers(models.TransientModel):
    _inherit = 'stock.assign.serial'

    def apply(self):
        self._assign_serial_numbers(False)