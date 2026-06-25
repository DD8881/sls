from scrapers.atb import ATBScraper
from scrapers.auchan import AuchanScraper
from scrapers.base import BaseScraper
from scrapers.fora import ForaScraper
from scrapers.fozzy import FozzyScraper
from scrapers.metro import MetroScraper
from scrapers.novus import NovusScraper
from scrapers.silpo import SilpoScraper
from scrapers.varus import VarusScraper


def get_scrapers() -> list[BaseScraper]:
    return [
        SilpoScraper(),
        VarusScraper(),
        ATBScraper(),
        ForaScraper(),
        FozzyScraper(),
        AuchanScraper(),
        MetroScraper(),
        NovusScraper(),
    ]
