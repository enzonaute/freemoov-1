{
    'name': 'Freemoov Livechat AI',
    'version': '17.0.0.1.10',
    'category': 'Website/Live Chat',
    'summary': 'Hybrid livechat bot: Odoo native chatbot + Claude fallback on open questions',
    'author': 'Freemoov',
    'website': 'https://freemoov.com',
    'depends': ['im_livechat', 'website_livechat', 'product', 'website_sale', 'website_freemoov', 'project', 'sms'],
    'external_dependencies': {'python': ['markdown']},
    'data': [
        'security/knowledge_security.xml',
        'security/ir.model.access.csv',
        'views/knowledge_article_views.xml',
        'data/ir_config_parameter_data.xml',
        'data/ir_cron_data.xml',
        'data/res_partner_data.xml',
        'views/res_config_settings_view.xml',
        'views/livechat_ai_log_view.xml',
        'views/product_cards_template.xml',
        'views/verification_view.xml',
        'views/menu.xml',
    ],
    # `assets_embed_core` and not `web.assets_frontend`: it is the bundle every
    # flavour of the widget is built from — the one served on freemoov.com
    # includes it, and so do the external and CORS embeds.
    'assets': {
        'im_livechat.assets_embed_core': [
            'freemoov_livechat_ai/static/src/scss/assistant_theme.scss',
            'freemoov_livechat_ai/static/src/js/assistant_avatar.js',
            'freemoov_livechat_ai/static/src/js/assistant_presentation.js',
            'freemoov_livechat_ai/static/src/js/assistant_typing.js',
            'freemoov_livechat_ai/static/src/js/assistant_suggestions.js',
            'freemoov_livechat_ai/static/src/xml/assistant_thread.xml',
        ],
    },
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
