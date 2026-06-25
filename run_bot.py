import logging

import db
from bot.app import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)


def main():
    db.init_db()
    app = create_app()
    logging.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
