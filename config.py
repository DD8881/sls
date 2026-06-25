import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# Private group/channel that receives user feedback (bot must be a member there).
# Find it via getUpdates after the bot posts once; for groups it is negative.
FEEDBACK_CHAT_ID = os.getenv("FEEDBACK_CHAT_ID", "")

DATABASE_PATH = os.getenv("DATABASE_PATH", "discounts.db")

SILPO_BRANCH_ID = os.getenv("SILPO_BRANCH_ID", "00000000-0000-0000-0000-000000000000")
NOVUS_STORE_ID = os.getenv("NOVUS_STORE_ID", "48201070")
METRO_STORE_ID = os.getenv("METRO_STORE_ID", "48215610")

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "100"))

PRODUCTS_PER_PAGE = int(os.getenv("PRODUCTS_PER_PAGE", "5"))
CATEGORIES_PER_PAGE = int(os.getenv("CATEGORIES_PER_PAGE", "8"))

WEBAPP_URL = os.getenv("WEBAPP_URL", "")
