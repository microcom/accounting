# Copyright 2024 Dixmit
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import re

from odoo import api, fields, models


class AccountReconcileModel(models.Model):
    _inherit = "account.reconcile.model"

    unique_matching = fields.Boolean(
        string="Unique match",
        help="If this box is checked, counterparts will only be suggested "
        "if only one possible counterpart is found.",
    )

    # ########################################################
    # RECONCILIATION CRITERIA
    # ########################################################

    def _apply_rules(self, st_line, partner):
        """Apply criteria to get candidates for all reconciliation models.

        :param st_line: The statement line to match.
        :param partner: The partner to consider.
        :return: A dict with:
            * aml_ids: A list of account.move.line ids.
            * model: An account.reconcile.model record (optional).
            * status: 'write_off' if write-off must be applied.
            * auto_reconcile: Flag indicating automatic reconciliation.
        """
        # In Odoo 19, 'trigger' replaces 'rule_type':
        #   'manual'        ~ old 'writeoff_button'
        #   'auto_reconcile' ~ old 'writeoff_suggestion' with auto_reconcile
        # We skip manual models that cannot be proposed automatically.
        available_models = self.filtered(
            lambda m: m.trigger == "auto_reconcile" or m.can_be_proposed
        ).sorted()

        for rec_model in available_models:
            if not rec_model._is_applicable_for(st_line, partner):
                continue
            return {
                "model": rec_model,
                "status": "write_off",
                "auto_reconcile": rec_model.trigger == "auto_reconcile",
            }
        return {}

    def _is_applicable_for(self, st_line, partner):
        """Returns True iff this model can be used for the given st_line."""
        self.ensure_one()

        # Filter on journals
        if (
            self.match_journal_ids
            and st_line.move_id.journal_id not in self.match_journal_ids
        ):
            return False

        # Filter on amount
        if (
            (
                self.match_amount == "lower"
                and abs(st_line.amount) >= self.match_amount_max
            )
            or (
                self.match_amount == "greater"
                and abs(st_line.amount) <= self.match_amount_min
            )
            or (
                self.match_amount == "between"
                and (
                    abs(st_line.amount) > self.match_amount_max
                    or abs(st_line.amount) < self.match_amount_min
                )
            )
        ):
            return False

        # Filter on partners
        if self.match_partner_ids and partner not in self.match_partner_ids:
            return False

        # Filter on label
        if self.match_label:
            rule_term = (self.match_label_param or "").lower()
            record_term = (st_line.payment_ref or "").lower()
            if (
                (self.match_label == "contains" and rule_term not in record_term)
                or (self.match_label == "not_contains" and rule_term in record_term)
                or (
                    self.match_label == "match_regex"
                    and not re.match(rule_term, record_term)
                )
            ):
                return False

        return True

    def _get_partner_from_mapping(self, st_line):
        """Return a partner from model mapping.

        In Odoo 19, partner_mapping_line_ids has been removed from the
        reconcile model. Always returns an empty partner recordset.
        """
        self.ensure_one()
        return self.env["res.partner"]

    def _get_write_off_move_lines_dict(self, residual_balance, partner_id, label=None):
        """Get move.lines dict for the reconciliation model write-off lines.

        :param residual_balance: The residual balance of the account.
        :param partner_id: The partner id.
        :param label: Optional label for regex matching.
        :return: A list of dicts representing move.lines to create.
        """
        self.ensure_one()

        currency = self.company_id.currency_id

        lines_vals_list = []
        for line in self.line_ids:
            balance = 0
            if line.amount_type == "percentage":
                balance = currency.round(residual_balance * (line.amount / 100.0))
            elif line.amount_type == "fixed":
                balance = currency.round(
                    line.amount * (1 if residual_balance > 0.0 else -1)
                )
            elif line.amount_type == "regex":
                m = re.findall(line.amount_string, label or "")
                if m:
                    extracted_amount = self._str2float(m[0])
                    sign = 1 if residual_balance > 0.0 else -1
                    balance = currency.round(extracted_amount * sign)
                else:
                    balance = 0.0
            else:
                balance = 0.0

            if currency.is_zero(balance):
                continue

            writeoff_line = {
                "name": line.label,
                "balance": balance,
                "debit": balance > 0 and balance or 0,
                "credit": balance < 0 and -balance or 0,
                "account_id": line.account_id.id,
                "currency_id": currency.id,
                "analytic_distribution": line.analytic_distribution,
                "reconcile_model_id": self.id,
                "tax_ids": [],
            }
            lines_vals_list.append(writeoff_line)
            residual_balance -= balance

            if line.tax_ids:
                taxes = line.tax_ids
                detected_fp = self.env["account.fiscal.position"]._get_fiscal_position(
                    self.env["res.partner"].browse(partner_id)
                )
                if detected_fp:
                    taxes = detected_fp.map_tax(taxes)
                writeoff_line["tax_ids"] += [(6, 0, taxes.ids)]

        return lines_vals_list

    @api.model
    def _str2float(self, amount_string):
        """Convert a string to float."""
        seps = [" ", ",", "."]
        for sep in seps:
            amount_string = amount_string[:-3].replace(sep, "") + amount_string[-3:]
        amount_string = amount_string.replace(",", ".")
        return float(amount_string)
