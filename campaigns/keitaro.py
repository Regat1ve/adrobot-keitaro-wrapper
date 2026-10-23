"""Тонкий клиент Keitaro Admin API v1: только те методы, которые нужны обёртке."""
import time

import requests
from django.conf import settings


class KeitaroError(Exception):
    pass


class Keitaro:
    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base = (base_url or settings.KEITARO_URL).rstrip('/') + '/admin_api/v1'
        self.session = requests.Session()
        self.session.headers['Api-Key'] = api_key or settings.KEITARO_API_KEY

    def _call(self, method: str, path: str, **kwargs):
        # GET и PUT повторяем: TLS у трекера иногда рвётся. POST нет, чтобы не создать кампанию дважды
        attempts = 1 if method == 'POST' else 3
        for attempt in range(attempts):
            try:
                resp = self.session.request(method, self.base + path, timeout=30, **kwargs)
                break
            except requests.RequestException as e:
                if attempt == attempts - 1:
                    raise KeitaroError(f'Keitaro недоступен ({method} {path}): {e.__class__.__name__}') from e
                time.sleep(1 + attempt)
        try:
            data = resp.json() if resp.content else None
        except ValueError:
            raise KeitaroError(f'{method} {path}: HTTP {resp.status_code}, ответ не JSON')
        if resp.status_code >= 400 or (isinstance(data, dict) and data.get('error')):
            message = data.get('error') if isinstance(data, dict) else resp.text[:200]
            raise KeitaroError(f'{method} {path}: {message}')
        return data

    # справочники
    def domains(self):
        return self._call('GET', '/domains')

    def campaign_groups(self):
        return self._call('GET', '/groups', params={'type': 'campaigns'})

    def traffic_sources(self):
        return self._call('GET', '/traffic_sources')

    def create_campaign_group(self, name: str):
        return self._call('POST', '/groups', json={'name': name, 'type': 'campaigns'})

    _offers_cache: tuple[float, list] = (0.0, [])

    def offers(self):
        # ponytail: кэш на процесс на минуту, при тысячах офферов перейти на серверный поиск Keitaro
        cached_at, offers = Keitaro._offers_cache
        if time.monotonic() - cached_at > 60:
            offers = self._call('GET', '/offers')
            Keitaro._offers_cache = (time.monotonic(), offers)
        return offers

    # кампании и потоки
    def campaigns(self):
        return self._call('GET', '/campaigns')

    def campaign(self, campaign_id: int):
        return self._call('GET', f'/campaigns/{campaign_id}')

    def create_campaign(self, **fields):
        return self._call('POST', '/campaigns', json=fields)

    def streams(self, campaign_id: int):
        return self._call('GET', f'/campaigns/{campaign_id}/streams')

    def create_stream(self, **fields):
        return self._call('POST', '/streams', json=fields)

    def set_stream_offers(self, stream_id: int, offers: list[dict]):
        """Полностью заменяет список офферов потока: [{'offer_id': 1, 'share': 50, 'state': 'active'}, ...]."""
        return self._call('PUT', f'/streams/{stream_id}', json={'offers': offers})
