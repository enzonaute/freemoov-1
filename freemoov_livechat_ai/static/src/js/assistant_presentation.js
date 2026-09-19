/** @odoo-module **/

import { ChatWindow } from "@mail/core/common/chat_window";
import { Composer } from "@mail/core/common/composer";
import { Thread } from "@mail/core/common/thread_model";
import { patch } from "@web/core/utils/patch";

export function isFreemoovThread(env, thread) {
    return thread?.type === "livechat" && Boolean(
        env.services["im_livechat.livechat"]?.options.freemoov_ai_bot_partner_id
    );
}

export function persistFreemoovOperator(env, thread, data) {
    const service = env.services["im_livechat.livechat"];
    if (isFreemoovThread(env, thread) && data.operator_pid &&
        service.thread?.id === thread.id && service.thread?.model === thread.model) {
        // Native session restoration reads this top-level tuple, not channel.operator.
        service.updateSession({operator_pid: data.operator_pid, channel: thread});
    }
}

patch(Thread, {
    _insert(data) {
        const thread = super._insert(...arguments);
        persistFreemoovOperator(this.env, thread, data);
        return thread;
    },
});

patch(ChatWindow.prototype, {
    get isFreemoovAssistant() {
        return isFreemoovThread(this.env, this.thread);
    },
    get freemoovAssistantSubtitle() {
        const botId = this.livechatService.options.freemoov_ai_bot_partner_id;
        return this.thread?.operator && this.thread.operator.id !== botId
            ? this.thread.operator.name : "Assistant IA";
    },
});

patch(Composer.prototype, {
    async processMessage(callback) {
        const composer = this.props.composer;
        // Keep native editing, attachments and non-assistant conversations intact.
        if (!this.isFreemoovAssistant || composer.message ||
            composer.attachments.length || !composer.textInputContent.trim()) {
            return super.processMessage(...arguments);
        }
        if (!this.state.active) {
            return;
        }
        const text = composer.textInputContent;
        const input = this.ref.el;
        this.state.active = false;
        // Do not clear attachments/mentions before _sendMessage has read them.
        // The RPC includes AI generation, so waiting for it leaves sent text
        // misleadingly visible in the disabled input for several seconds.
        composer.textInputContent = "";
        try {
            await callback(text);
        } catch (error) {
            if (!composer.textInputContent) {
                composer.textInputContent = text;
            }
            throw error;
        } finally {
            this.state.active = true;
            if (input?.isConnected) {
                input.focus();
            }
        }
        this.props.onPostCallback?.();
        // Never erase a newer draft populated while the request was pending.
        if (!composer.textInputContent) {
            this.clear();
        }
    },
    get isFreemoovAssistant() {
        return isFreemoovThread(this.env, this.thread);
    },
    get placeholder() {
        return this.isFreemoovAssistant ? "Écrivez votre message…" : super.placeholder;
    },
});
