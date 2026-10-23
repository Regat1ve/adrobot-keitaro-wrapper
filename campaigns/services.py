"""Логика обёртки над Keitaro. Вьюхи только вызывают эти функции."""
import re
import secrets

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from .keitaro import Keitaro, KeitaroError
from .models import Campaign, OfferLink, Stream
from .shares import split_shares

GOOGLE_URL = 'https://google.com'


class EditorError(Exception):
    pass


def offer_names(kt: Keitaro) -> dict[int, str]:
    return {o['id']: o['name'] for o in kt.offers()}


# --- синхронизация -------------------------------------------------------

@transaction.atomic
def fetch_streams(campaign: Campaign, kt: Keitaro) -> None:
    """Fetch streams from KT: Keitaro источник правды, неопубликованные правки сбрасываются.

    Офферы, о которых мы знали, но которых в Keitaro больше нет, не удаляются, а уходят в архив (removed).
    """
    names = offer_names(kt)
    kt_streams = kt.streams(campaign.kt_id)
    seen = []
    for s in kt_streams:
        stream, _ = Stream.objects.update_or_create(
            kt_id=s['id'],
            defaults={'campaign': campaign, 'name': s['name'], 'position': s.get('position') or 0,
                      'has_offers': bool(s.get('offers'))},
        )
        seen.append(stream.id)
        kt_offers = {o['offer_id']: o for o in s.get('offers') or []}
        local = {link.offer_id: link for link in stream.links.all()}
        for offer_id, o in kt_offers.items():
            link = local.get(offer_id) or OfferLink(stream=stream, offer_id=offer_id)
            link.offer_name = names.get(offer_id, link.offer_name)
            link.state = OfferLink.ACTIVE
            link.share = link.published_share = o['share']
            link.save()
        for offer_id, link in local.items():
            if offer_id in kt_offers:
                continue
            if link.state == OfferLink.NEW:  # так и не был опубликован, хранить нечего
                link.delete()
                continue
            link.state = OfferLink.REMOVED
            link.share = link.published_share = 0
            link.save()
        if local and not stream.has_offers:  # все офферы удалили в Keitaro, но архив остаётся у нас
            stream.has_offers = True
            stream.save(update_fields=['has_offers'])
    campaign.streams.exclude(id__in=seen).delete()
    campaign.fetched_at = timezone.now()
    campaign.save(update_fields=['fetched_at'])


def open_campaign(kt_id: int, kt: Keitaro) -> Campaign:
    campaign = Campaign.objects.filter(kt_id=kt_id).first()
    if campaign is None:
        data = kt.campaign(kt_id)
        campaign = Campaign.objects.create(kt_id=kt_id, name=data['name'])
        fetch_streams(campaign, kt)
    return campaign


# --- веса ----------------------------------------------------------------

def recalc(stream: Stream) -> None:
    """Закреплённые офферы держат свою долю, остальные живые делят остаток поровну, мёртвые получают 0."""
    links = list(stream.links.all())
    live = [link for link in links if link.is_live]
    pinned = [link for link in live if link.pinned]
    free = [link for link in live if not link.pinned]
    left = 100 - sum(link.share for link in pinned)
    if left < 0:
        raise EditorError(f'Закреплено {100 - left}%, больше 100%. Открепите или уменьшите доли.')
    for link, share in zip(free, split_shares(len(free), left)):
        link.share = share
    for link in links:
        if not link.is_live:
            link.share = 0
    OfferLink.objects.bulk_update(links, ['share'])


def validate_for_push(stream: Stream) -> None:
    live = stream.live_links
    if not live:
        raise EditorError('В потоке не осталось офферов. Верните оффер из архива или добавьте новый.')
    total = sum(link.share for link in live)
    if total != 100:
        raise EditorError(f'Сумма долей {total}%, нужно ровно 100%. Открепите часть офферов.')


# --- правки в потоке ------------------------------------------------------

@transaction.atomic
def add_offer(stream: Stream, offer_id: int, kt: Keitaro) -> OfferLink:
    names = offer_names(kt)
    if offer_id not in names:
        raise EditorError(f'Оффера {offer_id} нет в Keitaro')
    link = stream.links.filter(offer_id=offer_id).first()
    if link and link.is_live:
        raise EditorError(f'Оффер {offer_id} уже в потоке')
    if link:  # был в архиве или помечен на удаление: просто возвращаем
        return restore_offer(link)
    link = OfferLink.objects.create(stream=stream, offer_id=offer_id, offer_name=names[offer_id], state=OfferLink.NEW)
    if not stream.has_offers:
        stream.has_offers = True
        stream.save(update_fields=['has_offers'])
    recalc(stream)
    return link


@transaction.atomic
def remove_offer(link: OfferLink) -> None:
    """Remove: доля 0%, оффер серый. Неопубликованный новый оффер удаляется сразу."""
    stream = link.stream
    if link.state == OfferLink.NEW:
        link.delete()
    else:
        link.state = {OfferLink.ACTIVE: OfferLink.REMOVING, OfferLink.RESTORING: OfferLink.REMOVED}.get(link.state, link.state)
        link.pinned = False
        link.save(update_fields=['state', 'pinned'])
    recalc(stream)


@transaction.atomic
def restore_offer(link: OfferLink) -> OfferLink:
    """Bring back: оффер из архива снова получает трафик после публикации."""
    if link.state == OfferLink.REMOVING:
        link.state = OfferLink.ACTIVE
    elif link.state == OfferLink.REMOVED:
        link.state = OfferLink.RESTORING
    link.save(update_fields=['state'])
    recalc(link.stream)
    return link


@transaction.atomic
def pin_offer(link: OfferLink, share: int | None = None) -> None:
    """Закрепить долю оффера. Без share закрепляется текущая доля."""
    if not link.is_live:
        raise EditorError('Закрепить можно только оффер, который получает трафик')
    if share is not None:
        if not 0 < share <= 100:
            raise EditorError('Доля должна быть от 1 до 100')
        link.share = share
    link.pinned = True
    link.save(update_fields=['pinned', 'share'])
    recalc(link.stream)


@transaction.atomic
def unpin_offer(link: OfferLink) -> None:
    link.pinned = False
    link.save(update_fields=['pinned'])
    recalc(link.stream)


@transaction.atomic
def cancel(stream: Stream) -> None:
    """Отменить неопубликованные правки: поток снова совпадает с Keitaro."""
    for link in stream.links.all():
        if link.state == OfferLink.NEW:
            link.delete()
            continue
        if link.state == OfferLink.REMOVING:
            link.state = OfferLink.ACTIVE
        elif link.state == OfferLink.RESTORING:
            link.state = OfferLink.REMOVED
        link.share = link.published_share
        link.save(update_fields=['state', 'share'])


@transaction.atomic
def push(stream: Stream, kt: Keitaro) -> None:
    """Push to KT: отправляем в Keitaro только живые офферы, архив остаётся у нас."""
    validate_for_push(stream)
    live = stream.live_links
    kt.set_stream_offers(stream.kt_id, [{'offer_id': link.offer_id, 'share': link.share, 'state': 'active'}
                                        for link in live])
    for link in stream.links.all():
        if link.is_live:
            link.state = OfferLink.ACTIVE
            link.published_share = link.share
        else:
            link.state = OfferLink.REMOVED
            link.share = link.published_share = 0
        link.save(update_fields=['state', 'share', 'published_share'])


# --- создание кампании ----------------------------------------------------

def parse_geo(raw: str) -> list[str]:
    codes = [c.strip().upper() for c in re.split(r'[\s,;]+', raw) if c.strip()]
    bad = [c for c in codes if not re.fullmatch(r'[A-Z]{2}', c)]
    if not codes or bad:
        raise EditorError(f'Гео: нужны двухбуквенные коды стран через запятую, например AU,MX. Ошибка: {", ".join(bad) or "пусто"}')
    return list(dict.fromkeys(codes))


@transaction.atomic
def create_campaign(kt: Keitaro, *, name: str, geo: list[str], offer_id: int,
                    domain_id: int | None = None, group_id: int | None = None,
                    traffic_source_id: int | None = None) -> Campaign:
    """Кампания из двух потоков: гео -> google.com, все остальные -> выбранный оффер."""
    if offer_id not in offer_names(kt):
        raise EditorError(f'Оффера {offer_id} нет в Keitaro')
    fields = {'name': name, 'alias': f'{slugify(name) or "campaign"}-{secrets.token_hex(3)}', 'type': 'position'}
    for key, value in (('domain_id', domain_id), ('group_id', group_id), ('traffic_source_id', traffic_source_id)):
        if value:
            fields[key] = value
    data = kt.create_campaign(**fields)
    try:
        kt.create_stream(campaign_id=data['id'], type='regular', name=f'{",".join(geo)} -> Google', position=1,
                         schema='redirect', action_type='http', action_payload=GOOGLE_URL,
                         filters=[{'name': 'country', 'mode': 'accept', 'payload': geo}])
        kt.create_stream(campaign_id=data['id'], type='regular', name='Остальные -> оффер', position=2,
                         schema='landings', action_type='http',
                         offers=[{'offer_id': offer_id, 'share': 100, 'state': 'active'}])
    except KeitaroError as e:
        raise EditorError(f'Кампания {data["id"]} создана, но потоки не добавились: {e}') from e
    campaign = Campaign.objects.create(kt_id=data['id'], name=data['name'])
    fetch_streams(campaign, kt)
    return campaign
