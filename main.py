import os

if os.getenv('BOT_TYPE') == 'max':
    import max_bot
else:
    import telegram_bot
