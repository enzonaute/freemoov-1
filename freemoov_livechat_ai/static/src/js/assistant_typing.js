/** @odoo-module **/

import { Record } from "@mail/core/common/record";
import { Thread } from "@mail/core/common/thread";
import { Thread as ThreadModel } from "@mail/core/common/thread_model";
import { ThreadService, threadService } from "@mail/core/common/thread_service";

import { reactive, useState } from "@odoo/owl";

import { browser } from "@web/core/browser/browser";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { patch } from "@web/core/utils/patch";
import { isFreemoovThread } from "./assistant_presentation";

/**
 * How long the indicator survives the RPC that raised it.
 *
 * The assistant's message is already committed when the RPC returns, but it
 * reaches this browser over the bus, on its own connection: dropping the
 * indicator on the response would blink it off and let the answer pop in a
 * frame later. This is also the ceiling, because nothing else ends the wait
 * when no answer is coming at all — assistant disabled, rate limit hit, or a
 * human operator holding the conversation — so it may not outlast a pause a
 * visitor would find plausible.
 */
export const REPLY_GRACE_DELAY = 1500;
export const SLOW_REPLY_DELAY = 12000;

/**
 * Client-side "the assistant is writing" indicator.
 *
 * The server-side one cannot work for this assistant, and that is not a bug
 * left for later: `discuss.channel._freemoov_ai_notify_typing` fires before the
 * agent loop runs, but a bus notification is only flushed at commit, and the
 * whole turn — up to seven Anthropic calls — happens inside the transaction of
 * the visitor's own `message_post`. The notification therefore lands at the
 * same instant as the answer it was meant to announce, i.e. never in time.
 *
 * What the visitor's browser does know is that its `message_post` RPC has not
 * come back yet. That is the entire signal, and it happens to be exact: the
 * assistant answers synchronously, inside that request.
 */
export class AssistantTypingService {
    /**
     * Posts still in flight. A counter and not a flag: a visitor left waiting
     * fifteen seconds sends "?" again, and the second post must not let the
     * first one's grace period take the indicator down under it.
     */
    pending = 0;
    /** True while the grace period below is running. */
    grace = false;
    /** @type {number|null} */
    timeout = null;
    slowTimeout = null;
    slow = false;
    failed = false;

    constructor() {
        // Same shape as `im_livechat.chatbot`: components read this service
        // through `useState`, so its state has to be reactive to begin with.
        return reactive(this);
    }

    /** A message is on its way to the server. */
    start() {
        this.failed = false;
        if (this.pending === 0) {
            this.slow = false;
            this.slowTimeout = browser.setTimeout(() => {
                this.slow = this.pending > 0;
            }, SLOW_REPLY_DELAY);
        }
        this.pending++;
        this._clearGrace();
    }

    /** It arrived; an answer should follow. */
    postSucceeded() {
        this.pending = Math.max(0, this.pending - 1);
        if (this.pending > 0) {
            return;
        }
        this._clearSlow();
        this.grace = true;
        this.timeout = browser.setTimeout(() => this._clearGrace(), REPLY_GRACE_DELAY);
    }

    /** It did not; nothing is coming. */
    postFailed() {
        this.pending = Math.max(0, this.pending - 1);
        if (this.pending === 0) {
            this.failed = true;
            this._clearSlow();
            this._clearGrace();
        }
    }

    /**
     * @param {import("models").Thread} thread
     * @returns {boolean}
     */
    isWaitingFor(thread) {
        if (thread?.type !== "livechat") {
            return false;
        }
        if (this.pending > 0) {
            // An RPC is in flight, and for this assistant that request *is* the
            // wait. Nothing on screen can contradict it.
            return true;
        }
        // Grace period: end it early if the answer is already there. While the
        // visitor is the last to have spoken, nobody has answered yet — their
        // own message stays in the list throughout, `ThreadService.post`
        // inserting it optimistically before the RPC and swapping it for the
        // persisted one after.
        return this.grace && thread.newestMessage?.isSelfAuthored !== false;
    }

    _clearGrace() {
        browser.clearTimeout(this.timeout);
        this.timeout = null;
        this.grace = false;
    }

    _clearSlow() {
        browser.clearTimeout(this.slowTimeout);
        this.slowTimeout = null;
        this.slow = false;
    }
}

export const assistantTypingService = {
    start() {
        return new AssistantTypingService();
    },
};
registry.category("services").add("freemoov_livechat_ai.typing", assistantTypingService);

threadService.dependencies.push("freemoov_livechat_ai.typing");

patch(ThreadService.prototype, {
    setup(env, services) {
        super.setup(env, services);
        this.assistantTyping = services["freemoov_livechat_ai.typing"];
    },

    /**
     * Raise the indicator before the RPC, not after it: the RPC *is* the wait.
     */
    async post(thread) {
        if (!isFreemoovThread(this.env, thread)) {
            return super.post(...arguments);
        }
        this.assistantTyping.start();
        let message;
        try {
            message = await super.post(...arguments);
        } catch (error) {
            // Nothing was posted, so nothing is coming. Holding the bubble
            // through the grace period would tell the visitor their message is
            // being answered when it never even arrived.
            this.assistantTyping.postFailed();
            throw error;
        }
        if (!message) {
            // `im_livechat` returns nothing when the thread could not be
            // persisted — same reasoning as above.
            this.assistantTyping.postFailed();
            return message;
        }
        this.assistantTyping.postSucceeded();
        return message;
    },
});

patch(ThreadModel.prototype, {
    setup() {
        super.setup();
        /**
         * The bubble itself, as a transient message.
         *
         * Built exactly like `chatbotTypingMessage` and `livechatWelcomeMessage`
         * next door in `im_livechat/embed/common/thread_model_patch.js`: a
         * `Message` record that never reaches the server, rendered by the
         * native `Message` component with `isTypingMessage`, which swaps its
         * body for Odoo's own animated dots. So this is the native indicator,
         * triggered locally — not a bubble of our own that would drift from it.
         *
         * The id keeps clear of both neighbours (-0.1 and -0.2 offsets) while
         * staying negative, which is how the store tells a transient message
         * from a persisted one.
         */
        this.assistantTypingMessage = Record.one("Message", {
            compute() {
                if (this.type !== "livechat" || !this.operator) {
                    return;
                }
                return {
                    id: Number.isInteger(this.id) ? -0.3 - this.id : -0.3,
                    res_id: this.id,
                    model: this.model,
                    author: this.operator,
                };
            },
        });
    },
});

patch(Thread.prototype, {
    setup() {
        super.setup();
        this.assistantTyping = useState(useService("freemoov_livechat_ai.typing"));
    },

    get showAssistantTyping() {
        // An Odoo chatbot script owns the conversation while it runs, and it
        // has its own indicator (`chatbotService.isTyping`). The assistant is
        // not answering then either: the script's messages are posted by the
        // script's own partner, which `_freemoov_ai_human_active` reads as
        // somebody already handling the visitor, so the turn is skipped.
        if (this.chatbotService?.active) {
            return false;
        }
        return (
            this.assistantTyping.isWaitingFor(this.props.thread) &&
            Boolean(this.props.thread.assistantTypingMessage)
        );
    },
});
