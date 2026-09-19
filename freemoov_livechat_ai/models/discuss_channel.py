import json
import logging
from datetime import timedelta

import psycopg2
from markupsafe import Markup

from odoo import api, fields, models

from ..services import agent_loop
from ..services.anthropic_client import AnthropicClient, estimate_cost_eur
from ..services.message_format import format_assistant_text
from ..services.prompt_builder import build_messages_from_channel, build_system_prompt
from ..services.tools import is_dry_run
from ..services.tools.catalog import _serialize, _store_warehouses

_logger = logging.getLogger(__name__)

BOT_PARTNER_XMLID = "freemoov_livechat_ai.partner_ai_bot"
PRODUCT_CARDS_TEMPLATE = "freemoov_livechat_ai.assistant_product_cards"
MAX_PRODUCT_CARDS = 3
USAGE_WINDOW = timedelta(hours=1)
BUDGET_PAUSE_TEXT = (
    "Cette conversation a temporairement atteint sa limite d’utilisation. "
    "Vous pourrez poursuivre ici en réessayant plus tard, dans une heure au plus. "
    "Votre historique est conservé et vous n’avez pas besoin de recommencer la conversation."
)
RATE_PAUSE_TEXT = (
    "L’assistant reçoit beaucoup de demandes en ce moment. "
    "Merci de réessayer dans une minute : votre message est conservé ici."
)

# Re-entrancy marker carried by everything this module posts, read back in
# `_freemoov_ai_trigger_from_message`. Never read from the database: it lives
# for the length of the `message_post` call that set it.
BOT_POST_CONTEXT_KEY = "freemoov_ai_bot_post"

FALLBACK_TEXT = "Je n'ai pas pu formuler de réponse. Réessayez dans un instant ou contactez-nous via https://www.freemoov.com/contactus."

# Arguments the audit log must not keep verbatim. The log is readable by every
# internal user (`base.group_user`), and these values are exactly what a
# customer types to prove who they are: `identifiant` is their e-mail, phone or
# order reference, `reference_commande` the same datum typed into another tool,
# `code` the one-time code that unlocks their orders and invoices. The number is
# how many leading characters survive — enough to tell two calls of the same
# turn apart, not enough to rebuild the value; 0 drops it entirely, which is the
# only sane amount for a code (three characters of a six-digit code is half of
# it).
REDACTED_TOOL_ARGS = {"identifiant": 3, "reference_commande": 3, "code": 0, "question": 0}


def _int_param(ICP, key, default):
    """Integer configuration parameter, tolerant of what an admin typed.

    `_freemoov_ai_config` runs before the first `try` of the turn: a bare
    `int("abc")` there escapes to the caller and costs the visitor their answer
    without leaving so much as a log row.
    """
    raw = ICP.get_param(key)
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        _logger.warning("freemoov_ai: invalid integer in %s (%r), falling back to %s",
                        key, raw, default)
        return default


def _format_price(value):
    """`1299.99` -> `'1299,99'`, `1300.0` -> `'1300'`.

    Formatted here rather than with `formatLang`: the cards are rendered in the
    visitor's own environment, whose language is whatever their browser
    negotiated, and an English one would turn a Belgian price into `1,299.99`.
    """
    text = "%.2f" % (value or 0.0)
    if text.endswith(".00"):
        text = text[:-3]
    return text.replace(".", ",")


def _availability_label(product):
    """One line under the price, in the vocabulary the shop uses.

    Stores holding stock first (that is the question the visitor asks), then
    the online order, then nothing but a human. Never "indisponible": a
    product with no stock anywhere is still orderable most of the time
    (dropshipping covers the majority of the catalogue).
    """
    in_store = ["%s %s" % (store, qty) for store, qty in (product.get("dispo") or {}).items() if qty]
    if in_store:
        return " · ".join(in_store)
    return "Sur commande" if product.get("commandable") else "Nous contacter"


def _redact_tool_calls(tool_calls):
    """The calls as they go to the log: identifying arguments truncated.

    Works on copies. The dicts it is handed carry the arguments that were
    really executed, and a redaction leaking back into them would leave the
    audit trail describing a call nobody made.
    """
    redacted = []
    for tool_call in tool_calls:
        arguments = dict(tool_call.get("arguments") or {})
        for name, keep in REDACTED_TOOL_ARGS.items():
            if name in arguments:
                arguments[name] = "%s…" % str(arguments[name])[:keep]
        redacted.append({**tool_call, "arguments": arguments})
    return redacted


class DiscussChannel(models.Model):
    _inherit = "discuss.channel"

    freemoov_ai_visitor_partner_id = fields.Many2one(
        'res.partner', readonly=True, copy=False, ondelete='set null',
        help='Authenticated visitor who opened the AI widget; never proof of identity for tools.')

    @api.returns('mail.message', lambda message: message.id)
    def message_post(self, **kwargs):
        # Finish the visitor's native post, including its bus notification,
        # before posting any answer. mail.message.create is too early: the
        # answer used to reach the widget before the visitor's own message.
        message = super().message_post(**kwargs)
        try:
            self.sudo()._freemoov_ai_trigger_from_message(message.sudo())
        except psycopg2.Error:
            raise  # Preserve Odoo's transaction retry handling.
        except Exception:
            _logger.exception("freemoov_ai: trigger failed")
        return message

    def _freemoov_ai_is_enabled(self):
        ICP = self.env["ir.config_parameter"].sudo()
        return ICP.get_param("freemoov_livechat_ai.enabled") == "True"

    def _freemoov_ai_is_dry_run(self):
        return is_dry_run(self.env)

    def _freemoov_ai_config(self):
        ICP = self.env["ir.config_parameter"].sudo()
        return {
            "api_key": ICP.get_param("freemoov_livechat_ai.api_key") or "",
            "model": ICP.get_param("freemoov_livechat_ai.model") or "claude-haiku-4-5-20251001",
            "max_tokens": _int_param(ICP, "freemoov_livechat_ai.max_tokens", 1200),
            "rate_limit_per_min": _int_param(
                ICP, "freemoov_livechat_ai.rate_limit_per_min", 20
            ),
            # Rolling-hour ceiling, both directions summed. Below 1 lifts it.
            "conversation_token_budget": _int_param(
                ICP, "freemoov_livechat_ai.conversation_token_budget", 50000
            ),
            # Per-call HTTP timeout. It is what bounds the turn: the loop makes
            # up to MAX_TOOL_ITERATIONS + 1 calls, synchronously, inside the
            # visitor's own request, and the worker is killed at
            # `limit_time_real` (120s on Odoo.sh). 15 x 7 = 105s stays under it.
            "client_timeout": _int_param(ICP, "freemoov_livechat_ai.client_timeout", 15),
        }

    def _freemoov_ai_check_rate_limit(self, limit_per_min):
        Log = self.env["freemoov.livechat.ai.log"].sudo()
        window = fields.Datetime.now() - timedelta(minutes=1)
        count = Log.search_count([("create_date", ">=", window), ("status", "in", ("ok", "dry_run", "escalated"))])
        return count < limit_per_min

    def _freemoov_ai_log(self, status, **kw):
        return self.env["freemoov.livechat.ai.log"].sudo().create({
            "channel_id": self.id,
            "status": status,
            **kw,
        })

    def _freemoov_ai_human_active(self, look_back_seconds=120):
        """A human (non-bot, non-visitor) has posted in the last N seconds."""
        bot = self._freemoov_ai_bot_partner()
        # The bot remains a member after a transfer; assignment is durable,
        # unlike a time window which would let the AI interrupt later.
        if (self.livechat_channel_id and bot and bot in self.channel_member_ids.partner_id
                and self.livechat_operator_id and self.livechat_operator_id != bot):
            return True
        threshold = fields.Datetime.now() - timedelta(seconds=look_back_seconds)
        recent = self.message_ids.filtered(
            lambda m: m.create_date >= threshold
            and m.author_id
            and (not bot or m.author_id.id != bot.id)
            and not self._freemoov_ai_is_visitor_message(m)
            and m.message_type != "notification"
        )
        return bool(recent)

    def _freemoov_ai_is_visitor_message(self, message):
        if not message.author_id:
            return True  # Guest identity/membership is enforced by mail routes.
        partner = message.author_id.sudo()
        if partner == self._freemoov_ai_bot_partner():
            return False
        if (partner == self.sudo().freemoov_ai_visitor_partner_id
                and partner in self.sudo().channel_member_ids.partner_id):
            return True
        users = partner.user_ids.filtered(lambda user: user.active)
        return (partner in self.sudo().channel_member_ids.partner_id
                and bool(users) and all(user.share and not user._is_public() for user in users))

    def _freemoov_ai_handoff(self):
        self.ensure_one()
        if not self.livechat_channel_id:
            return False
        operator = self.livechat_channel_id.sudo().with_context(
            freemoov_ai_exclude_partner_id=self.freemoov_ai_visitor_partner_id.id)._get_operator(
            lang=self.env.context.get('lang'), country_id=self.country_id.id)
        if not operator:
            return False
        channel = self.sudo()
        channel.add_members(operator.partner_id.ids, open_chat_window=True,
                            post_joined_message=False)
        channel.livechat_operator_id = operator.partner_id
        channel._broadcast(operator.partner_id.ids)
        # The adviser broadcast does not reach the visitor (especially guests).
        # Send only the public operator identity, not privileged channel info.
        partner = operator.partner_id
        public_name = partner.user_livechat_username or partner.display_name
        payload = {'Thread': {
            'id': channel.id, 'model': 'discuss.channel',
            'operator_pid': [partner.id, public_name.replace(',', '')],
        }}
        self.env['bus.bus']._sendone(channel.uuid, 'mail.record/insert', payload)
        self.env['bus.bus']._sendone(channel, 'mail.record/insert', payload)
        return True

    def _freemoov_ai_bot_partner(self):
        try:
            return self.env.ref(BOT_PARTNER_XMLID, raise_if_not_found=False)
        except Exception:
            return False

    def _freemoov_ai_verified_partner_id(self):
        """Whose data this turn was allowed to touch — 0 when nobody proved
        anything.

        Read after the turn rather than before it: a visitor can complete their
        verification mid-turn (`envoyer_code`, then `verifier_code`), and what
        the audit needs is the state the tools actually ran under.
        """
        partner = self._freemoov_ai_verified_partner()
        return partner.id if partner else 0

    def _freemoov_ai_post_as_bot(self, body):
        """Post one message as the assistant. The only way this module posts.

        Three context keys, all load-bearing:

        * `guest=None`. A livechat turn runs as the public user with the
          visitor's guest in the context, and `message_post` answers that exact
          combination by forcing `author_id` to False and stamping the guest on
          the message instead — the `author_id` below would be dropped on the
          floor. The bot's own answer would then reach the browser as a message
          from the visitor, and come back through `message_post` looking
          like a new question: the assistant would answer itself, in a loop,
          one real API call per round.
        * `BOT_POST_CONTEXT_KEY`. The re-entrancy marker the trigger reads. It
          holds whatever the framework decides to do with the author, which is
          the whole point of having it on top of the fix above.
        * `temporary_id=None`. Only the visitor's post may acknowledge the
          optimistic browser message. Reusing its id on the bot response or
          cards replaces that question in the client-side store.
        """
        self.ensure_one()
        post_kwargs = {
            "body": body if isinstance(body, Markup) else format_assistant_text(body),
            "message_type": "comment",
            "subtype_xmlid": "mail.mt_comment",
        }
        bot = self._freemoov_ai_bot_partner()
        if bot:
            post_kwargs["author_id"] = bot.id
        channel = self.sudo().with_context(**{
            BOT_POST_CONTEXT_KEY: True, "guest": None, "temporary_id": None,
        })
        return channel.message_post(**post_kwargs)

    def _freemoov_ai_post_apology(self):
        """Tell the visitor the turn failed, best effort and nothing more.

        Without this the chat simply goes quiet: the bot announced it was
        typing, and then nothing ever comes. Everything is swallowed — this is
        the failure path, it may not fail in turn — everything except a
        `psycopg2.Error`, which is not a failed courtesy message but a dead
        cursor: swallowed here it would never reach Odoo's retrying layer, and
        the `error` log row the caller is about to return would be read through
        a connection that no longer exists.
        """
        try:
            self._freemoov_ai_post_as_bot(FALLBACK_TEXT)
        except psycopg2.Error:
            raise
        except Exception:
            _logger.exception("freemoov_ai: could not post the fallback message")

    def _freemoov_ai_fail(self, visitor_message_text, error, **kw):
        """Record the failed turn, then apologise — in that order.

        The log row is the audit trail and the meter the conversation budget is
        read from; the message to the visitor is courtesy. Courtesy does not
        get to make us lose the audit.
        """
        log = self._freemoov_ai_log(
            "error", visitor_message=visitor_message_text, error_message=str(error), **kw)
        if not self._freemoov_ai_is_dry_run():
            self._freemoov_ai_post_apology()
        return log

    def _freemoov_ai_tokens_spent(self):
        """Rolling-hour usage; a historic ceiling must never lock out a visitor."""
        Log = self.env["freemoov.livechat.ai.log"].sudo()
        [(spent_in, spent_out)] = Log._read_group(
            [("channel_id", "=", self.id),
             ("create_date", ">", fields.Datetime.now() - USAGE_WINDOW)],
            aggregates=["input_tokens:sum", "output_tokens:sum"],
        )
        return (spent_in or 0) + (spent_out or 0)

    def _freemoov_ai_pause(self, status, question, text):
        """No model call or human handoff; keep the reason and visible reply auditable."""
        log = self._freemoov_ai_log(status, visitor_message=question, bot_response=text)
        if not self._freemoov_ai_is_dry_run():
            self._freemoov_ai_post_as_bot(text)
        return log

    def _freemoov_ai_notify_typing(self, is_typing):
        """Typing indicator, over the bot's own membership (native bus).

        The bot has to be a member for the notification to carry a persona the
        visitor's client can display, so the first turn joins the channel.

        Two rules hold over the whole body rather than over one line of it:

        * everything reads and writes through `sudo`. A livechat turn answers
          in the **public visitor's** environment, where `channel_member_ids`
          raises outright — and a non-sudo read that merely came back empty
          would be worse, silently re-joining the channel every turn;
        * everything is guarded. This is decoration; its failure must never
          cost the answer that follows, nor the log row recording the tokens
          the turn has already spent.
        """
        if self._freemoov_ai_is_dry_run():
            # Nothing will be posted, so nothing may be announced — and the
            # join below would show the bot to the operators of a channel it
            # is not going to answer.
            return
        try:
            bot = self._freemoov_ai_bot_partner()
            if not bot:
                return
            channel = self.sudo()
            member = channel.channel_member_ids.filtered(lambda m: m.partner_id == bot)
            if not member:
                channel.add_members(partner_ids=bot.ids, post_joined_message=False)
                member = channel.channel_member_ids.filtered(lambda m: m.partner_id == bot)
            member._notify_typing(is_typing)
        except psycopg2.Error:
            raise
        except Exception:
            _logger.debug("freemoov_ai: typing notify failed", exc_info=True)

    def _freemoov_ai_post_product_cards(self, product_ids):
        """Post the cards for the products the turn actually talked about.

        Re-checked against the database rather than trusted: the ids travelled
        through the model, and publication can change mid-conversation.
        """
        tmpls = self.env["product.template"].sudo().browse(product_ids).exists()
        tmpls = tmpls.filtered(lambda t: t.is_published and t.active)[:MAX_PRODUCT_CARDS]
        if not tmpls:
            return
        # Second pass through `_serialize`, after the tool's own: the card must
        # show the price and the availability as they are now, from the one
        # helper that knows how to read them, rather than a copy of what the
        # model was told a few seconds ago.
        warehouse_map = _store_warehouses(self.env)
        products = []
        for tmpl in tmpls:
            product = _serialize(self.env, tmpl, warehouse_map)
            product["prix_label"] = "%s € TVAC" % _format_price(product["prix_tvac"])
            product["dispo_label"] = _availability_label(product)
            products.append(product)
        html = self.env["ir.qweb"].sudo()._render(PRODUCT_CARDS_TEMPLATE, {"products": products})
        self._freemoov_ai_post_as_bot(html)

    def _freemoov_ai_respond(self, visitor_message_text):
        """Run the AI flow for this channel and post the response.
        Returns the log record.
        """
        self.ensure_one()
        cfg = self._freemoov_ai_config()

        if not self._freemoov_ai_is_enabled():
            return self._freemoov_ai_log("skipped_disabled", visitor_message=visitor_message_text)

        if self._freemoov_ai_human_active():
            return self._freemoov_ai_log("skipped_human", visitor_message=visitor_message_text)

        if not self._freemoov_ai_check_rate_limit(cfg["rate_limit_per_min"]):
            return self._freemoov_ai_pause("skipped_rate", visitor_message_text, RATE_PAUSE_TEXT)

        # Limit recent spend without permanently disabling long conversations.
        # Paused turns consume no tokens and cannot extend the rolling window.
        budget = cfg["conversation_token_budget"]
        if budget > 0 and self._freemoov_ai_tokens_spent() >= budget:
            return self._freemoov_ai_pause("skipped_budget", visitor_message_text, BUDGET_PAUSE_TEXT)

        try:
            system_prompt = build_system_prompt(self.env)
            messages = build_messages_from_channel(self)
        except Exception as e:
            _logger.exception("freemoov_ai: failed to build prompt")
            return self._freemoov_ai_fail(visitor_message_text, e)

        client = AnthropicClient(
            api_key=cfg["api_key"],
            model=cfg["model"],
            max_tokens=cfg["max_tokens"],
            timeout=cfg["client_timeout"],
        )
        self._freemoov_ai_notify_typing(True)
        try:
            out = agent_loop.run_agent(self.env, self, client, system_prompt, messages,
                                       visitor_message_text=visitor_message_text)
        except psycopg2.Error:
            # The cursor is gone: Odoo's retrying layer has to see this one.
            # Nothing else may touch the database on the way out either — a
            # typing notification or an error log written here would raise in
            # turn and bury the failure that caused it.
            raise
        except Exception as e:
            _logger.exception("freemoov_ai: agent loop failed")
            self._freemoov_ai_notify_typing(False)
            return self._freemoov_ai_fail(
                visitor_message_text, e,
                verified_partner_id=self._freemoov_ai_verified_partner_id())
        self._freemoov_ai_notify_typing(False)

        text, escalate = out["text"], out["escalate"]
        cost = estimate_cost_eur(out["input_tokens"], out["output_tokens"])
        turn_kw = {
            "tools_used": ", ".join(dict.fromkeys(c["name"] for c in out["tool_calls"])),
            "knowledge_sources": out.get('knowledge_sources', []),
            "tool_calls_json": json.dumps(
                _redact_tool_calls(out["tool_calls"]), ensure_ascii=False, default=str
            ),
            "verified_partner_id": self._freemoov_ai_verified_partner_id(),
        }

        if self._freemoov_ai_is_dry_run():
            return self._freemoov_ai_log(
                "dry_run",
                visitor_message=visitor_message_text,
                bot_response=text,
                model=cfg["model"],
                input_tokens=out["input_tokens"],
                output_tokens=out["output_tokens"],
                cost_eur=cost,
                latency_ms=out["api_latency_ms"],
                **turn_kw,
            )

        # The loop always fills `text` when it forces an escalation, so this is
        # a last net rather than a live path — but an empty bubble is the one
        # failure a visitor cannot make sense of.
        body = text if text else FALLBACK_TEXT
        if escalate:
            # Never repeat an unverified availability/transfer promise from
            # the model. The assignment result is the sole source of truth.
            if self._freemoov_ai_handoff():
                body = "💬 Votre conversation a été transmise à un conseiller connecté."
            else:
                body = "Aucun conseiller n’est connecté actuellement. Contactez-nous via https://www.freemoov.com/contactus."

        self._freemoov_ai_post_as_bot(body)

        if out["product_ids"] and not escalate:
            # Not under an escalation: the cards would illustrate an answer the
            # bot has just admitted it could not give, and the visitor would
            # read them as the recommendation nobody made.
            try:
                self._freemoov_ai_post_product_cards(out["product_ids"])
            except psycopg2.Error:
                raise
            except Exception:
                # Illustration, posted after the answer the visitor came for.
                # It must not cost the turn its log row: the tokens are spent
                # either way, and the conversation budget is counted from
                # exactly those rows.
                _logger.exception("freemoov_ai: product cards failed")

        return self._freemoov_ai_log(
            "escalated" if escalate else "ok",
            visitor_message=visitor_message_text,
            bot_response=text,
            model=cfg["model"],
            input_tokens=out["input_tokens"],
            output_tokens=out["output_tokens"],
            cost_eur=cost,
            latency_ms=out["api_latency_ms"],
            **turn_kw,
        )

    def _freemoov_ai_trigger_from_message(self, message):
        """Called after a visitor message is posted. Triggers AI if conditions met.

        Everything here answers one question: is this message the visitor's?
        Answering our own would not merely be silly, it would recurse — the
        answer is posted before the log row that meters the rate limit exists,
        so nothing downstream would stop it.
        """
        channel = self.browse(message.res_id) if message.model == "discuss.channel" else self
        if not channel or channel.channel_type != "livechat":
            return
        if self.env.context.get(BOT_POST_CONTEXT_KEY):
            # Our own message, still inside the `message_post` that created it.
            return
        # Only react to visitor messages (no author_id = public website visitor)
        if not channel._freemoov_ai_is_visitor_message(message):
            return
        # `comment` is what a visitor's message is; everything else on a
        # livechat channel is machinery (notifications, joins, transfers).
        if message.message_type != "comment":
            return
        # Strip HTML for logging
        from ..services.prompt_builder import strip_html
        text = strip_html(message.body or "")
        if not text or len(text) < 2:
            return
        if not channel._freemoov_ai_bot_partner():
            # Without the bot partner nothing this module posts carries an
            # author, and an authorless message is exactly what we take for a
            # visitor two lines above. Staying silent is the only guard that
            # does not depend on the framework's choice of fallback author.
            _logger.error(
                "freemoov_ai: %s is missing — the assistant cannot tell its own "
                "messages apart from the visitor's and will not answer",
                BOT_PARTNER_XMLID,
            )
            return
        channel._freemoov_ai_respond(text)
