"""The channel side of a turn: budget, loop, audit log, cards, typing.

Mocked at the same boundary as the loop tests — `create_message` — so every
tool below really runs against the test database and nothing leaves the box.
"""
import json
from datetime import timedelta
from unittest.mock import call, patch

import psycopg2

from odoo.tests import tagged
from odoo import fields

from ..models.discuss_channel import BOT_POST_CONTEXT_KEY
from ..services import agent_loop, tools
from ..services.anthropic_client import AnthropicClient
from ..services.tools.catalog import _WAREHOUSE_MAP_PARAM
from .common import FreemoovAiCase
from .test_agent_loop import _resp

CHANNEL_LOGGER = "odoo.addons.freemoov_livechat_ai.models.discuss_channel"


@tagged("post_install", "-at_install", "freemoov_ai")
class TestRespondFlow(FreemoovAiCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ICP = cls.env["ir.config_parameter"].sudo()
        ICP.set_param("freemoov_livechat_ai.enabled", "True")
        ICP.set_param("freemoov_livechat_ai.dry_run", "False")
        ICP.set_param("freemoov_livechat_ai.api_key", "test-key")

    def _log(self):
        return self.env["freemoov.livechat.ai.log"].sudo()

    def _bodies(self):
        return [str(m.body) for m in self.channel.message_ids]

    def _published_product(self, name="Carte Trott", price=999.0, **values):
        return self.env["product.template"].create(dict({
            "name": name, "list_price": price,
            "is_published": True, "sale_ok": True,
        }, **values))

    def _pin_warehouses(self, mapping):
        """Freeze the store -> warehouse mapping: the cards must not depend on
        the warehouses that happen to exist in the database."""
        self.env["ir.config_parameter"].sudo().set_param(
            _WAREHOUSE_MAP_PARAM, json.dumps(mapping))

    def _card_body(self):
        return next(b for b in self._bodies() if "fm-assistant-card" in b)

    def _post_cards_for(self, tmpl):
        """One full turn whose answer carries a card for `tmpl`."""
        responses = [
            _resp(tool_use=("fiche_produit", {"product_id": tmpl.id})),
            _resp(text="Voici la fiche : https://www.freemoov.com%s" % tmpl.website_url),
        ]
        with patch.object(AnthropicClient, "create_message", side_effect=responses):
            return self.channel._freemoov_ai_respond("montre %s" % tmpl.name)

    def _verify_channel(self, email="cliente.fictive@example.be"):
        """A verified visitor — fictional identity, created here and nowhere
        else. `_send_code` is mocked: nothing is ever sent."""
        partner = self.env["res.partner"].create({"name": "Cliente Fictive AI", "email": email})
        Verification = self.env["freemoov.livechat.verification"]
        with patch.object(type(Verification), "_send_code") as send:
            Verification._start_verification(self.channel, email)
            code = send.call_args.args[2]  # (partner, method, code)
        self.assertTrue(Verification._check_code(self.channel, code)["verified"])
        return partner

    # -- boucle nominale --------------------------------------------------
    def test_respond_logs_tools_and_posts(self):
        responses = [
            _resp(tool_use=("infos_magasins", {})),
            _resp(text="Nos trois magasins sont ouverts du mardi au samedi."),
        ]
        with patch.object(AnthropicClient, "create_message", side_effect=responses):
            log = self.channel._freemoov_ai_respond("vos horaires ?")
        self.assertEqual(log.status, "ok")
        self.assertEqual(log.tools_used, "infos_magasins")
        self.assertIn('"ok": true', log.tool_calls_json)
        self.assertIn("mardi", self._bodies()[0])
        # Usage and timing come from the whole turn, not from the last call.
        self.assertEqual((log.input_tokens, log.output_tokens), (20, 10))
        # The loop renamed this key; reading `latency_ms` would silently log 0.
        self.assertEqual(log.latency_ms, 100)

    def test_repeated_tools_are_listed_once_in_order(self):
        responses = [
            _resp(tool_use=("infos_magasins", {"ville": "namur"})),
            _resp(tool_use=("chercher_produits", {"recherche": "rien"})),
            _resp(tool_use=("infos_magasins", {"ville": "liege"})),
            _resp(text="Voilà."),
        ]
        with patch.object(AnthropicClient, "create_message", side_effect=responses):
            log = self.channel._freemoov_ai_respond("magasins et produits ?")
        self.assertEqual(log.tools_used, "infos_magasins, chercher_produits")
        self.assertEqual(len(json.loads(log.tool_calls_json)), 3)

    def test_empty_answer_still_says_something(self):
        """Last net: the loop is not supposed to return an empty text any more
        (it forces a sentence when it escalates), but an empty bubble would be
        the worst possible failure mode, so the fallback stays.
        """
        blank = {"text": "", "escalate": False, "product_ids": [], "tool_calls": [],
                 "input_tokens": 1, "output_tokens": 1, "api_latency_ms": 1}
        with patch.object(agent_loop, "run_agent", return_value=blank):
            self.channel._freemoov_ai_respond("?")
        self.assertIn("https://www.freemoov.com/contactus", self._bodies()[0])

    # -- cartes produit ---------------------------------------------------
    def test_product_cards_posted(self):
        tmpl = self._published_product()
        responses = [
            _resp(tool_use=("fiche_produit", {"product_id": tmpl.id})),
            _resp(text="Voici la fiche : https://www.freemoov.com%s" % tmpl.website_url),
        ]
        with patch.object(AnthropicClient, "create_message", side_effect=responses):
            self.channel._freemoov_ai_respond("montre la Carte Trott")
        bodies = self._bodies()
        self.assertTrue(any("fm-assistant-card" in b for b in bodies))
        self.assertTrue(any("Carte Trott" in b and "999" in b for b in bodies))

    def test_no_cards_when_the_turn_escalates(self):
        """Cards under "je vous passe un conseiller" would contradict the
        sentence they sit beneath: the escalation says the turn did not land.
        """
        tmpl = self._published_product(name="Carte Escalade")
        responses = [
            _resp(tool_use=("fiche_produit", {"product_id": tmpl.id})),
            _resp(text="Je préfère vous passer un conseiller. [ESCALATE]"),
        ]
        with patch.object(AnthropicClient, "create_message", side_effect=responses):
            log = self.channel._freemoov_ai_respond("cette trottinette ?")
        self.assertEqual(log.status, "escalated")
        self.assertFalse(any("fm-assistant-card" in b for b in self._bodies()))

    def test_cards_skip_unpublished_products(self):
        """Ids travel through the model, and publication can change mid-turn."""
        tmpl = self._published_product(name="Carte Retiree")
        blank = {"text": "Voici.", "escalate": False, "product_ids": [tmpl.id],
                 "tool_calls": [], "input_tokens": 1, "output_tokens": 1,
                 "api_latency_ms": 1}
        tmpl.is_published = False
        with patch.object(agent_loop, "run_agent", return_value=blank):
            self.channel._freemoov_ai_respond("?")
        self.assertFalse(any("fm-assistant-card" in b for b in self._bodies()))

    def test_card_price_keeps_its_cents(self):
        """`%.0f` rounded 1299,99 up to « 1300 € » — an announced price the
        shop does not charge, on the 59% of the catalogue that has cents.
        """
        tmpl = self._published_product(name="Carte Centimes", price=1299.99)
        self._post_cards_for(tmpl)
        body = self._card_body()
        self.assertIn("1299,99 € TVAC", body)
        self.assertNotIn("1300", body)

    def test_card_price_drops_empty_cents(self):
        tmpl = self._published_product(name="Carte Ronde", price=1300.0)
        self._post_cards_for(tmpl)
        body = self._card_body()
        self.assertIn("1300 € TVAC", body)
        self.assertNotIn("1300,00", body)

    def test_card_lists_the_stores_holding_stock(self):
        warehouse = self.env["stock.warehouse"].create(
            {"name": "Entrepot Carte AI", "code": "CRDA"})
        self._pin_warehouses({"liege": warehouse.id, "namur": None, "charleroi": None})
        tmpl = self._published_product(name="Carte Stock", price=500.0,
                                       detailed_type="product")
        self.env["stock.quant"].sudo()._update_available_quantity(
            tmpl.product_variant_ids[0], warehouse.lot_stock_id, 2)
        self._post_cards_for(tmpl)
        self.assertIn("Liège 2", self._card_body())

    def test_card_says_on_order_when_no_store_holds_it(self):
        self._pin_warehouses({"liege": None, "namur": None, "charleroi": None})
        tmpl = self._published_product(name="Carte Commande", price=500.0)
        self._post_cards_for(tmpl)
        self.assertIn("Sur commande", self._card_body())

    def test_card_falls_back_to_contact_us(self):
        """Neither in a store nor orderable online: the card must not imply
        the visitor can buy it in one click."""
        warehouse = self.env["stock.warehouse"].create(
            {"name": "Entrepot Vide AI", "code": "VIDA"})
        self._pin_warehouses({"liege": warehouse.id, "namur": None, "charleroi": None})
        website = self.env["website"].sudo().get_current_website()
        website.warehouse_id = self.env["stock.warehouse"].create(
            {"name": "Entrepot Web AI", "code": "WEBA"}).id
        tmpl = self._published_product(name="Carte Rupture", price=500.0,
                                       detailed_type="product",
                                       allow_out_of_stock_order=False)
        self._post_cards_for(tmpl)
        self.assertIn("Nous contacter", self._card_body())

    def test_failed_cards_do_not_cost_the_turn_its_log(self):
        """The cards are an illustration posted after the answer. The tokens
        are spent either way, and the budget is counted from the log rows.
        """
        tmpl = self._published_product(name="Carte Cassee")
        responses = [
            _resp(tool_use=("fiche_produit", {"product_id": tmpl.id})),
            _resp(text="Voici la fiche."),
        ]
        Channel = type(self.env["discuss.channel"])
        with patch.object(Channel, "_freemoov_ai_post_product_cards",
                          side_effect=RuntimeError("template gone")):
            with patch.object(AnthropicClient, "create_message", side_effect=responses):
                log = self.channel._freemoov_ai_respond("montre la Carte Cassee")
        self.assertEqual(log.status, "ok")
        self.assertIn("Voici la fiche.", self._bodies()[0])

    # -- budget de conversation -------------------------------------------
    def test_budget_stops_the_turn_before_any_api_call(self):
        self._log().create({"channel_id": self.channel.id, "status": "ok",
                            "input_tokens": 40000, "output_tokens": 10001})
        with patch.object(AnthropicClient, "create_message") as create_message:
            log = self.channel._freemoov_ai_respond("encore une question")
        self.assertEqual(log.status, "skipped_budget")
        self.assertFalse(create_message.called)
        self.assertIn("une heure", self._bodies()[0])
        self.assertTrue(log.bot_response)
        self.assertNotIn("conseiller", log.bot_response)

    def test_old_usage_does_not_block_conversation_forever(self):
        old = self._log().create({"channel_id": self.channel.id, "status": "ok",
                                  "input_tokens": 60000})
        # create_date is a protected audit field: age this test fixture in SQL.
        old.flush_recordset()
        self.env.cr.execute('UPDATE freemoov_livechat_ai_log SET create_date = %s WHERE id = %s',
                            (fields.Datetime.now() - timedelta(hours=2), old.id))
        old.invalidate_recordset(['create_date'])
        with patch.object(AnthropicClient, "create_message", return_value=_resp(text="Nous continuons ici.")) as api:
            log = self.channel._freemoov_ai_respond("encore une question")
        self.assertEqual(log.status, "ok")
        self.assertTrue(api.called)

    def test_recent_usage_still_counts_and_skips_do_not_extend_pause(self):
        self._log().create({"channel_id": self.channel.id, "status": "ok", "input_tokens": 50000})
        with patch.object(AnthropicClient, "create_message") as api:
            self.channel._freemoov_ai_respond("question")
            self.channel._freemoov_ai_respond("question suivante")
        self.assertFalse(api.called)
        self.assertEqual(self.channel._freemoov_ai_tokens_spent(), 50000)
        self.assertEqual(len(self._bodies()), 2)

    def test_rate_limit_posts_a_retry_notice_without_handoff(self):
        self.env['ir.config_parameter'].sudo().set_param('freemoov_livechat_ai.rate_limit_per_min', '1')
        self._log().create({"channel_id": self.channel.id, "status": "ok"})
        with patch.object(AnthropicClient, "create_message") as api:
            log = self.channel._freemoov_ai_respond("bonjour")
        self.assertFalse(api.called)
        self.assertEqual(log.status, 'skipped_rate')
        self.assertIn('une minute', self._bodies()[0])
        self.assertNotIn('conseiller', log.bot_response)

    def test_dry_run_budget_notice_is_only_logged(self):
        self.env['ir.config_parameter'].sudo().set_param('freemoov_livechat_ai.dry_run', 'True')
        self._log().create({"channel_id": self.channel.id, "status": "ok", "input_tokens": 50000})
        with patch.object(AnthropicClient, "create_message") as api:
            log = self.channel._freemoov_ai_respond("bonjour")
        self.assertFalse(api.called)
        self.assertTrue(log.bot_response)
        self.assertFalse(self._bodies())

    def test_budget_is_per_channel(self):
        other = self.env["discuss.channel"].create({
            "name": "Autre visiteur", "channel_type": "livechat",
            "livechat_operator_id": self.env.ref("base.partner_admin").id,
        })
        self._log().create({"channel_id": other.id, "status": "ok",
                            "input_tokens": 90000, "output_tokens": 0})
        with patch.object(AnthropicClient, "create_message",
                          side_effect=[_resp(text="Bonjour !")]):
            log = self.channel._freemoov_ai_respond("bonjour")
        self.assertEqual(log.status, "ok")

    def test_budget_can_be_raised_by_configuration(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "freemoov_livechat_ai.conversation_token_budget", "200000")
        self._log().create({"channel_id": self.channel.id, "status": "ok",
                            "input_tokens": 60000, "output_tokens": 0})
        with patch.object(AnthropicClient, "create_message",
                          side_effect=[_resp(text="Bonjour !")]):
            log = self.channel._freemoov_ai_respond("bonjour")
        self.assertEqual(log.status, "ok")

    # -- journal d'audit --------------------------------------------------
    def test_identifying_arguments_are_truncated_in_the_audit_log(self):
        """The log is readable by any internal user: what the visitor typed to
        identify themselves is kept as a correlation prefix, not verbatim.
        """
        identifier = "visiteur.fictif@example.be"
        responses = [
            _resp(tool_use=("envoyer_code", {"identifiant": identifier})),
            _resp(text="Je ne retrouve pas ce client."),
        ]
        Verification = self.env["freemoov.livechat.verification"]
        with patch.object(type(Verification), "_send_code"):
            with patch.object(AnthropicClient, "create_message", side_effect=responses):
                log = self.channel._freemoov_ai_respond("mes commandes")
        self.assertNotIn(identifier, log.tool_calls_json)
        self.assertNotIn("example.be", log.tool_calls_json)
        self.assertEqual(json.loads(log.tool_calls_json)[0]["arguments"],
                         {"identifiant": "vis…"})

    def test_the_executed_arguments_are_not_redacted(self):
        """Redaction happens on the way to the log, never on the way to the
        tool — a truncated identifier would resolve to nobody.
        """
        identifier = "visiteur.fictif@example.be"
        responses = [
            _resp(tool_use=("envoyer_code", {"identifiant": identifier})),
            _resp(text="Non trouvé."),
        ]
        Verification = self.env["freemoov.livechat.verification"]
        with patch.object(type(Verification), "_send_code"):
            with patch.object(tools, "run_tool", wraps=tools.run_tool) as run_tool:
                with patch.object(AnthropicClient, "create_message", side_effect=responses):
                    self.channel._freemoov_ai_respond("mes commandes")
        self.assertEqual(run_tool.call_args.args[3], {"identifiant": identifier})

    def test_order_references_are_truncated_too(self):
        """Same datum as `identifiant`, typed into another tool: an order
        reference identifies a customer just as well.
        """
        responses = [
            _resp(tool_use=("renvoyer_facture", {"reference_commande": "SO99999"})),
            _resp(text="Je ne retrouve pas cette commande."),
        ]
        with patch.object(AnthropicClient, "create_message", side_effect=responses):
            log = self.channel._freemoov_ai_respond("ma facture")
        self.assertNotIn("SO99999", log.tool_calls_json)
        self.assertEqual(json.loads(log.tool_calls_json)[0]["arguments"],
                         {"reference_commande": "SO9…"})

    def test_the_verified_customer_is_recorded(self):
        partner = self._verify_channel()
        with patch.object(AnthropicClient, "create_message",
                          side_effect=[_resp(text="Bonjour !")]):
            log = self.channel._freemoov_ai_respond("bonjour")
        self.assertEqual(log.verified_partner_id, partner.id)

    def test_an_anonymous_turn_records_no_customer(self):
        with patch.object(AnthropicClient, "create_message",
                          side_effect=[_resp(text="Bonjour !")]):
            log = self.channel._freemoov_ai_respond("bonjour")
        self.assertEqual(log.verified_partner_id, 0)

    def test_verification_code_never_reaches_the_log(self):
        responses = [
            _resp(tool_use=("verifier_code", {"code": "123456"})),
            _resp(text="Code invalide."),
        ]
        with patch.object(AnthropicClient, "create_message", side_effect=responses):
            log = self.channel._freemoov_ai_respond("mon code est 123456")
        self.assertNotIn("123456", log.tool_calls_json)

    # -- erreurs ----------------------------------------------------------
    def test_database_errors_reach_the_retrying_layer(self):
        """Swallowed, a `psycopg2.Error` would be written to a log through a
        dead cursor and turn one failed turn into a cascade of 500s.
        """
        before = self._log().search_count([("channel_id", "=", self.channel.id)])
        with patch.object(agent_loop, "run_agent",
                          side_effect=psycopg2.OperationalError("cursor is closed")):
            with patch.object(AnthropicClient, "create_message"):
                with self.assertRaises(psycopg2.OperationalError):
                    self.channel._freemoov_ai_respond("?")
        self.assertEqual(
            self._log().search_count([("channel_id", "=", self.channel.id)]), before)

    def test_other_loop_failures_are_logged_not_raised(self):
        with patch.object(agent_loop, "run_agent", side_effect=RuntimeError("boom")):
            log = self.channel._freemoov_ai_respond("?")
        self.assertEqual(log.status, "error")
        self.assertIn("boom", log.error_message)

    def test_a_failed_turn_still_says_something_to_the_visitor(self):
        """A timeout used to leave the visitor in front of a silent chat: the
        bot had said it was typing, then nothing ever came.
        """
        with patch.object(agent_loop, "run_agent", side_effect=RuntimeError("boom")):
            self.channel._freemoov_ai_respond("?")
        self.assertIn("https://www.freemoov.com/contactus", self._bodies()[0])
        self.assertNotIn("je transfère", self._bodies()[0])
        bot = self.env.ref("freemoov_livechat_ai.partner_ai_bot")
        self.assertEqual(self.channel.message_ids[0].author_id, bot)

    def test_the_apology_never_crashes_the_crash(self):
        """Best effort and nothing more: the failure path may not fail."""
        Channel = type(self.env["discuss.channel"])
        with patch.object(Channel, "message_post", side_effect=RuntimeError("post down")):
            with patch.object(agent_loop, "run_agent", side_effect=RuntimeError("boom")):
                log = self.channel._freemoov_ai_respond("?")
        self.assertEqual(log.status, "error")

    def test_the_apology_still_lets_database_errors_through(self):
        """The other half of the rule, and the exception to the `except
        Exception` above: a dead cursor is not a failed courtesy message.

        Swallowed here, the `SerializationFailure` would never reach Odoo's
        retrying layer — the turn would return an `error` log row read through
        a cursor that no longer exists, and the request would 500 later with
        the cause lost. Same guard as the three others in the file.
        """
        Channel = type(self.env["discuss.channel"])
        with patch.object(Channel, "message_post",
                          side_effect=psycopg2.OperationalError("cursor is closed")):
            with patch.object(agent_loop, "run_agent", side_effect=RuntimeError("boom")):
                with self.assertRaises(psycopg2.OperationalError):
                    self.channel._freemoov_ai_respond("?")

    def test_a_failed_channel_join_does_not_cost_the_answer(self):
        """`add_members` sits on the typing path, which is decoration. Its
        failure used to take down the answer *and* the log row that records
        the tokens already spent.
        """
        Channel = type(self.env["discuss.channel"])
        with patch.object(Channel, "add_members", side_effect=RuntimeError("no join")):
            with patch.object(AnthropicClient, "create_message",
                              side_effect=[_resp(text="Bonjour !")]):
                log = self.channel._freemoov_ai_respond("bonjour")
        self.assertEqual(log.status, "ok")
        self.assertIn("Bonjour !", self._bodies()[0])

    def test_typing_reads_its_membership_as_the_visitor(self):
        """Livechat runs in the public visitor's environment. A non-sudo read
        of `channel_member_ids` can come back empty there — silently, no
        error — and the indicator would be a no-op re-joining the channel on
        every single turn.
        """
        channel = self.channel.with_user(self.env.ref("base.public_user"))
        Member = type(self.env["discuss.channel.member"])
        with patch.object(Member, "_notify_typing") as notify:
            channel._freemoov_ai_notify_typing(True)
        self.assertEqual(notify.call_args_list, [call(True)])

    def test_the_channel_is_joined_once_not_once_per_turn(self):
        Channel = type(self.env["discuss.channel"])
        with patch.object(Channel, "add_members", wraps=self.channel.sudo().add_members) as join:
            with patch.object(AnthropicClient, "create_message",
                              side_effect=[_resp(text="Un."), _resp(text="Deux.")]):
                self.channel._freemoov_ai_respond("un")
                self.channel._freemoov_ai_respond("deux")
        self.assertEqual(join.call_count, 1)

    def test_trigger_relays_database_errors(self):
        Channel = type(self.env["discuss.channel"])
        with patch.object(Channel, "_freemoov_ai_trigger_from_message",
                          side_effect=psycopg2.OperationalError("cursor is closed")):
            with self.assertRaises(psycopg2.OperationalError), self.env.cr.savepoint():
                self.channel.message_post(body="coucou", message_type="comment",
                                          subtype_xmlid="mail.mt_comment")

    def test_trigger_swallows_everything_else(self):
        """A bug in the assistant must never cost the visitor their message."""
        Channel = type(self.env["discuss.channel"])
        with patch.object(Channel, "_freemoov_ai_trigger_from_message",
                          side_effect=RuntimeError("boom")):
            message = self.channel.message_post(body="coucou", message_type="comment",
                                                subtype_xmlid="mail.mt_comment")
        self.assertTrue(message.exists())

    # -- l'assistant ne se répond pas à lui-même --------------------------
    def _guest_channel(self):
        """The channel as a livechat turn really sees it: the public user, and
        the visitor's guest in the context — the exact combination
        `message_post` answers by dropping the author it is handed.
        """
        guest = self.env["mail.guest"].create({"name": "Visiteur Fictif AI"})
        return (self.channel
                .with_user(self.env.ref("base.public_user"))
                .with_context(guest=guest))

    def _visitor_message(self, text="bonjour", **values):
        """Use the real posting path, including the visitor notification."""
        return self._guest_channel().sudo().message_post(**dict({
            "body": text,
            "message_type": "comment",
            "subtype_id": self.env.ref("mail.mt_comment").id,
        }, **values))

    def test_the_bot_answer_is_authored_by_the_bot_not_by_the_visitor(self):
        """`message_post` forces `author_id` to False and stamps the guest on
        the message as soon as the current user is public and a guest sits in
        the context — which is every livechat turn. The answer reached the
        browser as a message from the visitor, and came back through
        `message_post` looking exactly like a new question.
        """
        message = self._guest_channel()._freemoov_ai_post_as_bot("Bonjour !").sudo()
        self.assertEqual(message.author_id,
                         self.env.ref("freemoov_livechat_ai.partner_ai_bot"))
        self.assertFalse(message.author_guest_id)

    def test_the_assistant_does_not_answer_its_own_message(self):
        """The recursion had nothing downstream to stop it: the answer is
        posted before the log row the rate limit and the token budget are both
        counted from.
        """
        Channel = type(self.env["discuss.channel"])
        with patch.object(Channel, "_freemoov_ai_respond") as respond:
            self._guest_channel()._freemoov_ai_post_as_bot("Bonjour, je suis l'assistant.")
        self.assertFalse(respond.called)

    def test_a_message_marked_as_ours_never_starts_a_turn(self):
        """The marker holds whatever the framework decides to do with the
        author, which is why it is worth having on top of the fix above.
        """
        Channel = type(self.env["discuss.channel"])
        with patch.object(Channel, "_freemoov_ai_respond") as respond:
            self._guest_channel().sudo().with_context(**{BOT_POST_CONTEXT_KEY: True}).message_post(
                body="Bonjour, je suis l'assistant.", message_type="comment",
                subtype_xmlid="mail.mt_comment")
        self.assertFalse(respond.called)

    def test_a_visitor_message_does_start_a_turn(self):
        """The control the three guards above are measured against."""
        Channel = type(self.env["discuss.channel"])
        with patch.object(Channel, "_freemoov_ai_respond") as respond:
            self._visitor_message("bonjour")
        self.assertEqual(respond.call_args.args, ("bonjour",))

    def test_a_missing_bot_partner_silences_the_assistant(self):
        """Without the bot partner nothing this module posts carries an author,
        and an authorless message is exactly what the trigger takes for a
        visitor. Not answering at all is the only guard that does not depend
        on the framework's choice of fallback author.
        """
        Channel = type(self.env["discuss.channel"])
        with patch.object(Channel, "_freemoov_ai_bot_partner", return_value=False):
            with patch.object(Channel, "_freemoov_ai_respond") as respond:
                with self.assertLogs(CHANNEL_LOGGER, "ERROR") as logs:
                    self._visitor_message("bonjour")
        self.assertFalse(respond.called)
        self.assertIn("partner_ai_bot", logs.output[0])

    def test_notifications_never_start_a_turn(self):
        """Joins, transfers and status lines are posted on the channel too."""
        Channel = type(self.env["discuss.channel"])
        with patch.object(Channel, "_freemoov_ai_respond") as respond:
            self._visitor_message("bonjour", message_type="notification")
        self.assertFalse(respond.called)

    # -- frappe -----------------------------------------------------------
    def test_typing_is_notified_around_the_turn(self):
        Member = type(self.env["discuss.channel.member"])
        with patch.object(Member, "_notify_typing") as notify:
            with patch.object(AnthropicClient, "create_message",
                              side_effect=[_resp(text="Bonjour !")]):
                self.channel._freemoov_ai_respond("bonjour")
        self.assertEqual(notify.call_args_list, [call(True), call(False)])
        bot = self.env.ref("freemoov_livechat_ai.partner_ai_bot")
        self.assertIn(bot, self.channel.channel_member_ids.partner_id)

    def test_typing_stops_when_the_turn_fails(self):
        Member = type(self.env["discuss.channel.member"])
        with patch.object(Member, "_notify_typing") as notify:
            with patch.object(agent_loop, "run_agent", side_effect=RuntimeError("boom")):
                self.channel._freemoov_ai_respond("?")
        self.assertEqual(notify.call_args_list, [call(True), call(False)])

    def test_no_typing_notification_on_a_dead_cursor(self):
        """Nothing may touch the database on the way out: a second query would
        raise from the `finally` and bury the original failure.
        """
        Member = type(self.env["discuss.channel.member"])
        with patch.object(Member, "_notify_typing") as notify:
            with patch.object(agent_loop, "run_agent",
                              side_effect=psycopg2.OperationalError("cursor is closed")):
                with self.assertRaises(psycopg2.OperationalError):
                    self.channel._freemoov_ai_respond("?")
        self.assertEqual(notify.call_args_list, [call(True)])

    # -- configuration ----------------------------------------------------
    def test_client_timeout_comes_from_configuration(self):
        """Seven synchronous API calls sit inside the visitor's request, under
        a 120s worker limit: the per-call timeout is what bounds the turn.
        """
        seen = []

        def _capture(client, system_prompt, messages, tools=None):
            seen.append(client.timeout)
            return _resp(text="Bonjour !")

        with patch.object(AnthropicClient, "create_message", autospec=True,
                          side_effect=_capture):
            self.channel._freemoov_ai_respond("bonjour")
            self.env["ir.config_parameter"].sudo().set_param(
                "freemoov_livechat_ai.client_timeout", "8")
            self.channel._freemoov_ai_respond("encore")
        self.assertEqual(seen, [15, 8])

    def test_a_malformed_parameter_falls_back_to_its_default(self):
        """A typo in a config parameter must not kill the turn before the log
        is even reachable — `_freemoov_ai_config` runs outside every `try`.
        """
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("freemoov_livechat_ai.client_timeout", "abc")
        ICP.set_param("freemoov_livechat_ai.conversation_token_budget", "beaucoup")
        seen = []

        def _capture(client, system_prompt, messages, tools=None):
            seen.append(client.timeout)
            return _resp(text="Bonjour !")

        with patch.object(AnthropicClient, "create_message", autospec=True,
                          side_effect=_capture):
            log = self.channel._freemoov_ai_respond("bonjour")
        self.assertEqual(log.status, "ok")
        self.assertEqual(seen, [15])

    def test_budget_zero_lifts_the_ceiling(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "freemoov_livechat_ai.conversation_token_budget", "0")
        self._log().create({"channel_id": self.channel.id, "status": "ok",
                            "input_tokens": 999999, "output_tokens": 0})
        with patch.object(AnthropicClient, "create_message",
                          side_effect=[_resp(text="Bonjour !")]):
            log = self.channel._freemoov_ai_respond("bonjour")
        self.assertEqual(log.status, "ok")

    def test_a_negative_budget_lifts_the_ceiling_too(self):
        """What the settings screen writes when an administrator asks for no
        ceiling: a 0 cannot be stored (the framework deletes the parameter and
        the default comes straight back), so -1 is what the field offers.
        """
        self.env["ir.config_parameter"].sudo().set_param(
            "freemoov_livechat_ai.conversation_token_budget", "-1")
        self._log().create({"channel_id": self.channel.id, "status": "ok",
                            "input_tokens": 999999, "output_tokens": 0})
        with patch.object(AnthropicClient, "create_message",
                          side_effect=[_resp(text="Bonjour !")]):
            log = self.channel._freemoov_ai_respond("bonjour")
        self.assertEqual(log.status, "ok")

    def test_dry_run_announces_nothing_to_the_visitor(self):
        """Nothing will be posted, so nothing may be announced: neither a
        typing indicator nor the channel join it needs.
        """
        self.env["ir.config_parameter"].sudo().set_param(
            "freemoov_livechat_ai.dry_run", "True")
        Member = type(self.env["discuss.channel.member"])
        with patch.object(Member, "_notify_typing") as notify:
            with patch.object(AnthropicClient, "create_message",
                              side_effect=[_resp(text="Bonjour !")]):
                self.channel._freemoov_ai_respond("bonjour")
        self.assertFalse(notify.called)
        bot = self.env.ref("freemoov_livechat_ai.partner_ai_bot")
        self.assertNotIn(bot, self.channel.channel_member_ids.partner_id)

    def test_dry_run_posts_no_cards(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "freemoov_livechat_ai.dry_run", "True")
        tmpl = self._published_product(name="Carte Muette")
        log = self._post_cards_for(tmpl)
        self.assertEqual(log.status, "dry_run")
        self.assertFalse(self._bodies())

    def test_dry_run_posts_no_apology(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "freemoov_livechat_ai.dry_run", "True")
        with patch.object(agent_loop, "run_agent", side_effect=RuntimeError("boom")):
            log = self.channel._freemoov_ai_respond("?")
        self.assertEqual(log.status, "error")
        self.assertFalse(self._bodies())

    def test_dry_run_logs_the_audit_without_posting(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "freemoov_livechat_ai.dry_run", "True")
        responses = [
            _resp(tool_use=("infos_magasins", {})),
            _resp(text="Nos trois magasins."),
        ]
        with patch.object(AnthropicClient, "create_message", side_effect=responses):
            log = self.channel._freemoov_ai_respond("vos horaires ?")
        self.assertEqual(log.status, "dry_run")
        self.assertEqual(log.tools_used, "infos_magasins")
        self.assertEqual(log.latency_ms, 100)
        self.assertFalse(self._bodies())
