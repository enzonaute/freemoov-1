from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install', 'freemoov_ai')
class TestBrowserWidget(HttpCase):
    def test_widget_welcome_has_accessible_actions_and_brand(self):
        params = self.env['ir.config_parameter'].sudo()
        for key, value in {
            'web.base.url': self.base_url(),
            'freemoov_livechat_ai.enabled': 'True',
            'freemoov_livechat_ai.dry_run': 'False',
            'freemoov_livechat_ai.api_key': 'unused-browser-test',
        }.items():
            params.set_param(key, value)
        channel = self.env['im_livechat.channel'].create({'name': 'Widget regression'})
        self.browser_js('/im_livechat/support/%s' % channel.id, code="""
            (async () => {
                function find(selector, root = document) {
                    const match = root.querySelector(selector);
                    if (match) return match;
                    for (const element of root.querySelectorAll('*')) {
                        if (element.shadowRoot) {
                            const nested = find(selector, element.shadowRoot);
                            if (nested) return nested;
                        }
                    }
                }
                async function waitFor(selector) {
                    for (let i = 0; i < 100; i++) {
                        const element = find(selector);
                        if (element) return element;
                        await new Promise(resolve => setTimeout(resolve, 100));
                    }
                    throw new Error('Missing widget element: ' + selector);
                }
                (await waitFor('.o-livechat-LivechatButton')).click();
                const window = await waitFor('.o-mail-ChatWindow');
                const welcome = await waitFor('.fm-assistant-welcome');
                if (!window.textContent.includes('Assistant IA')) throw new Error('AI identity not visible');
                const actions = [...welcome.querySelectorAll('button')];
                if (actions.length !== 3 || actions.some(button => !button.textContent.trim())) {
                    throw new Error('Expected three named welcome actions');
                }
                if (window.scrollWidth > window.clientWidth + 1) throw new Error('Horizontal overflow');
                const rect = window.getBoundingClientRect();
                if (rect.width > globalThis.innerWidth + 1 || rect.height > globalThis.innerHeight + 1) {
                    throw new Error('Widget exceeds viewport');
                }
                if ([...window.querySelectorAll('.o-mail-Message')].some(message => message.getClientRects().length)) {
                    throw new Error('Generic welcome message duplicates the welcome screen');
                }
                const { persistFreemoovOperator } = odoo.loader.modules.get('@freemoov_livechat_ai/js/assistant_presentation');
                const thread = {id: 987, model: 'discuss.channel', type: 'livechat'};
                let saved;
                const service = {options: {freemoov_ai_bot_partner_id: 100}, thread,
                    updateSession(values) { saved = values; }};
                const env = {services: {'im_livechat.livechat': service}};
                persistFreemoovOperator(env, thread, {operator_pid: [101, 'Conseiller']});
                if (saved?.operator_pid?.[0] !== 101 || saved.channel !== thread) throw new Error('Handoff operator not persisted');
                saved = undefined;
                persistFreemoovOperator(env, {...thread, id: 988}, {operator_pid: [102, 'Other']});
                if (saved) throw new Error('Other conversation overwrote visitor session');
                const { AssistantTypingService, SLOW_REPLY_DELAY } = modulesForTyping();
                function modulesForTyping() {
                    return odoo.loader.modules.get('@freemoov_livechat_ai/js/assistant_typing');
                }
                const { browser } = odoo.loader.modules.get('@web/core/browser/browser');
                const originalSetTimeout = browser.setTimeout;
                const originalClearTimeout = browser.clearTimeout;
                const timers = new Map();
                let timerId = 0;
                browser.setTimeout = (callback, delay) => {
                    timers.set(++timerId, {callback, delay});
                    return timerId;
                };
                browser.clearTimeout = id => timers.delete(id);
                try {
                    const typing = new AssistantTypingService();
                    const livechat = {type: 'livechat', newestMessage: {isSelfAuthored: true}};
                    typing.start();
                    const slowTimer = [...timers.values()].find(t => t.delay === SLOW_REPLY_DELAY);
                    slowTimer.callback();
                    if (!typing.slow || !typing.isWaitingFor(livechat)) throw new Error('Slow request loses waiting state');
                    typing.start();
                    typing.postSucceeded();
                    if (!typing.isWaitingFor(livechat)) throw new Error('Concurrent request hides wait');
                    typing.postFailed();
                    if (typing.isWaitingFor(livechat) || !typing.failed || typing.slow || timers.size) throw new Error('Failure did not clean timers');
                    typing.start();
                    if (typing.failed) throw new Error('New send retains old failure');
                    typing.postSucceeded();
                    if (!typing.isWaitingFor(livechat)) throw new Error('Reply grace missing');
                    livechat.newestMessage.isSelfAuthored = false;
                    if (typing.isWaitingFor(livechat)) throw new Error('Answer does not end grace');
                    for (const timer of [...timers.values()]) timer.callback();
                    if (timers.size) throw new Error('Timer leak after response');
                } finally {
                    browser.setTimeout = originalSetTimeout;
                    browser.clearTimeout = originalClearTimeout;
                }
                console.log('test successful');
            })().catch(error => console.error(error));
        """, ready="Boolean(window.odoo?.loader?.modules.has('@freemoov_livechat_ai/js/assistant_typing'))", timeout=60)


@tagged('post_install', '-at_install', 'freemoov_ai')
class TestBrowserWidgetSmallMobile(TestBrowserWidget):
    browser_size = '320x640'


@tagged('post_install', '-at_install', 'freemoov_ai')
class TestBrowserWidgetTablet(TestBrowserWidget):
    browser_size = '768x1024'
