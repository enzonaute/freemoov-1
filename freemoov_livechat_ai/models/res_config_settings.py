from odoo import fields, models

from ..services.tools import DRY_RUN_PARAM, is_dry_run

# The switches this module reads as `value == "True"`, and the field that
# displays each of them. The framework reads a boolean parameter as
# `bool(value)` instead, so it shows the shipped "False" as ticked: every
# switch below is therefore read back by hand in `get_values`.
SWITCH_FIELDS = {
    "freemoov_ai_knowledge_enabled": "freemoov_livechat_ai.knowledge_enabled",
    "freemoov_ai_enabled": "freemoov_livechat_ai.enabled",
    "freemoov_ai_verification_test_mode": "freemoov_livechat_ai.verification_test_mode",
    "freemoov_ai_repair_tool_enabled": "freemoov_livechat_ai.repair_tool_enabled",
}


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    freemoov_ai_knowledge_enabled = fields.Boolean(
        string="Activer la recherche documentaire validée",
        config_parameter="freemoov_livechat_ai.knowledge_enabled")

    freemoov_ai_enabled = fields.Boolean(
        string="Enable AI livechat assistant",
        config_parameter="freemoov_livechat_ai.enabled",
    )
    # No `config_parameter` on this one, unlike every other field here: see
    # `get_values` / `set_values` at the bottom of the class. The framework
    # can neither read nor write a boolean whose absent state means True.
    freemoov_ai_dry_run = fields.Boolean(
        string="Dry run (log only, do not post responses)",
        default=True,
        help="L'assistant réfléchit, appelle ses outils de lecture et écrit "
             "son journal, mais ne poste rien au visiteur et refuse tout "
             "outil qui envoie quelque chose. Mode d'observation.",
    )
    freemoov_ai_api_key = fields.Char(
        string="Anthropic API key",
        config_parameter="freemoov_livechat_ai.api_key",
    )
    freemoov_ai_model = fields.Char(
        string="Model",
        config_parameter="freemoov_livechat_ai.model",
        default="claude-haiku-4-5-20251001",
    )
    freemoov_ai_max_tokens = fields.Integer(
        string="Max response tokens",
        config_parameter="freemoov_livechat_ai.max_tokens",
        default=1200,
        help="Une réponse coupée est reformulée en une seconde tentative, "
             "sans rejouer les outils ni transfert automatique.",
    )
    freemoov_ai_client_timeout = fields.Integer(
        string="Timeout par appel API (s)",
        config_parameter="freemoov_livechat_ai.client_timeout",
        default=15,
        help="Un tour enchaîne jusqu'à 7 appels synchrones dans la requête du "
             "visiteur, sous la limite de 120 s du worker : ce délai est ce "
             "qui borne le tour.",
    )
    freemoov_ai_rate_limit_per_min = fields.Integer(
        string="Max calls per minute (global)",
        config_parameter="freemoov_livechat_ai.rate_limit_per_min",
        default=20,
    )
    freemoov_ai_conversation_token_budget = fields.Integer(
        string="Plafond de jetons par conversation / heure glissante",
        config_parameter="freemoov_livechat_ai.conversation_token_budget",
        default=50000,
        help="Jetons envoyés et reçus sur la dernière heure de cette conversation. "
             "Une pause explicite remplace le silence ; les anciens jetons expirent automatiquement. "
             "Saisir -1 pour lever le plafond : 0 n'est pas enregistrable, "
             "l'interface le relit comme « valeur par défaut ».",
    )
    freemoov_ai_verification_test_mode = fields.Boolean(
        string="2FA en mode test (code lisible dans le backend)",
        config_parameter="freemoov_livechat_ai.verification_test_mode",
        help="Le code à 6 chiffres n'est plus envoyé par e-mail ni par SMS : "
             "il est écrit sur la vérification, lisible par les seuls "
             "administrateurs. À n'activer qu'en recette.",
    )
    freemoov_ai_repair_tool_enabled = fields.Boolean(
        string="Activer le suivi de réparation",
        config_parameter="freemoov_livechat_ai.repair_tool_enabled",
        help="Tant que la correspondance étapes FSM -> libellés client n'est "
             "pas remplie, l'outil annonce au client des noms d'étapes internes.",
    )
    freemoov_ai_api_token = fields.Char(
        string="Token API assistant (vocal)",
        config_parameter="freemoov_livechat_ai.api_token",
        help="Secret partagé attendu dans l'en-tête X-Assistant-Token. "
             "Vider ce champ ferme l'API : elle répond 403.",
    )
    freemoov_ai_api_rate_per_min = fields.Integer(
        string="Max appels API / minute",
        config_parameter="freemoov_livechat_ai.api_rate_per_min",
        default=60,
    )

    def get_values(self):
        res = super().get_values()
        ICP = self.env["ir.config_parameter"].sudo()
        for name, key in SWITCH_FIELDS.items():
            res[name] = ICP.get_param(key) == "True"
        # Not in the loop above: this one is on unless the parameter says
        # otherwise, which is also why its field carries no `config_parameter`.
        res["freemoov_ai_dry_run"] = is_dry_run(self.env)
        return res

    def set_values(self):
        super().set_values()
        # Written as an explicit string, because `set_param` *deletes* the
        # parameter for a falsy value and a missing `dry_run` is read as dry
        # run everywhere else: bound through `config_parameter`, unticking the
        # box would have saved without changing anything at all.
        self.env["ir.config_parameter"].sudo().set_param(
            DRY_RUN_PARAM, "True" if self.freemoov_ai_dry_run else "False")
